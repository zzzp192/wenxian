"""Bounded, multi-source literature retrieval engine with validated PDF output."""
import hashlib
import io
import os
import re
import socket
import ipaddress
import tempfile
import time
from pathlib import Path
from urllib.parse import urlparse, quote, urljoin

import requests
from pypdf import PdfReader

MAX_BYTES = 120 * 1024 * 1024
MAX_PIPELINE_TIMEOUT = 300  # 5 minutes maximum per paper (user rule)


def get_email():
    import json
    if os.environ.get('LITERATURE_CONTACT_EMAIL'):
        return os.environ['LITERATURE_CONTACT_EMAIL']
    try:
        folder = Path(os.environ.get('LITERATURE_DATA_DIR', str(Path(__file__).resolve().parent / 'data')))
        cfg = json.loads((folder / 'settings.json').read_text(encoding='utf-8'))
        if cfg.get('email'):
            return cfg['email']
    except (OSError, ValueError):
        pass
    return 'datzh@live.cn'


def is_allowed_ip(ip_str):
    try:
        ip = ipaddress.ip_address(ip_str)
        if ip.is_loopback or ip.is_link_local:
            return False
        if ip.version == 4:
            if (ip in ipaddress.ip_network('10.0.0.0/8') or
                ip in ipaddress.ip_network('172.16.0.0/12') or
                ip in ipaddress.ip_network('192.168.0.0/16') or
                ip in ipaddress.ip_network('169.254.0.0/16')):
                return False
        elif ip.version == 6:
            if ip.is_private or ip.is_reserved:
                # allow 2001:2::/48 benchmarking fake-ip
                if not (ip in ipaddress.ip_network('2001:2::/48')):
                    return False
        return True
    except ValueError:
        return False


def public_get(url, timeout=(10, 35)):
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Accept': 'application/pdf,application/json,text/html,*/*;q=0.8'
    }
    for _ in range(6):
        parsed = urlparse(url)
        if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError('Unsupported download URL')
        target_port = parsed.port or (443 if parsed.scheme == 'https' else 80)
        try:
            addresses = socket.getaddrinfo(parsed.hostname, target_port, type=socket.SOCK_STREAM)
            if not addresses or any(not is_allowed_ip(a[4][0]) for a in addresses):
                raise ValueError('Private network downloads are not allowed')
        except socket.gaierror:
            pass

        response = requests.get(url, timeout=timeout, stream=True, allow_redirects=False,
                                headers=headers, verify=False)
        if response.is_redirect:
            redirect_target = response.headers.get('Location')
            if not redirect_target:
                break
            url = urljoin(url, redirect_target)
            response.close()
            continue
        response.raise_for_status()
        parts, length = [], 0
        try:
            for chunk in response.iter_content(65536):
                length += len(chunk)
                if length > MAX_BYTES:
                    raise ValueError('File exceeds size limit')
                parts.append(chunk)
            return b''.join(parts)
        finally:
            response.close()
    raise ValueError('Too many redirects or failed to fetch')


def metadata(url):
    import json
    return json.loads(public_get(url, timeout=(8, 15)))


def candidates(ident):
    """Generate candidate URLs across OA, Sci-Hub, Patents, and Academic Repositories."""
    raw = str(ident or '').strip()

    # arXiv bare or prefixed
    arxiv_bare = re.fullmatch(r'(?:arxiv:|10\.48550/arxiv\.)?(\d{4}\.\d{4,5}(?:v\d+)?)', raw, re.I)
    if arxiv_bare:
        yield f'https://arxiv.org/pdf/{arxiv_bare[1]}', 'arXiv'
        return

    # 1. Direct PDF or Patent or Repository URLs
    if raw.startswith(('http://', 'https://')):
        # arXiv
        arxiv_match = re.search(r'arxiv\.org/(?:abs|pdf)/(\d{4}\.\d{4,5}(?:v\d+)?)', raw, re.I)
        if arxiv_match:
            yield f'https://arxiv.org/pdf/{arxiv_match[1]}', 'arXiv'
            return

        # Google Patents
        pat_match = re.search(r'patents\.google\.com/patent/([A-Z0-9]+)', raw, re.I)
        if pat_match:
            pat_id = pat_match.group(1)
            try:
                r = requests.get(f'https://patents.google.com/patent/{pat_id}/en', headers={'User-Agent': 'Mozilla/5.0'}, timeout=10)
                from bs4 import BeautifulSoup
                soup = BeautifulSoup(r.text, 'html.parser')
                for a in soup.find_all('a', href=True):
                    if '.pdf' in a['href']:
                        yield a['href'], f'Google Patent ({pat_id})'
                        break
            except Exception:
                pass

        # NPL Eprints
        npl_match = re.search(r'eprintspublications\.npl\.co\.uk/(\d+)', raw)
        if npl_match:
            try:
                r = requests.get(f'https://eprintspublications.npl.co.uk/{npl_match.group(1)}/', headers={'User-Agent': 'Mozilla/5.0'}, timeout=10)
                from bs4 import BeautifulSoup
                soup = BeautifulSoup(r.text, 'html.parser')
                for a in soup.find_all('a', href=True):
                    if '.pdf' in a['href']:
                        yield a['href'], 'NPL Eprints'
                        break
            except Exception:
                pass

        if raw.lower().endswith('.pdf') or 'download' in raw.lower():
            yield raw, 'Direct PDF Link'

        # Check if URL contains DOI
        doi_in_url = re.search(r'10\.\d{4,9}/[^\s<>"\x00-\x1f]{1,260}', raw)
        if doi_in_url:
            raw = doi_in_url.group(0)

    # 2. Clean DOI
    doi = re.sub(r'^https?://(?:dx\.)?doi\.org/|^doi:\s*', '', raw, flags=re.I).strip()
    if doi.startswith('10.'):
        # Sci-Hub Mirror Tier (Fast & Comprehensive for pre-2023)
        yield f'https://sci.bban.top/pdf/{doi}.pdf', 'Sci-Hub Direct'
        
        # Sci-Hub double encoded parentheses handling e.g. %2528 and %2529
        if '(' in doi or ')' in doi:
            encoded_paren_doi = doi.replace('(', '%2528').replace(')', '%2529')
            yield f'https://sci.bban.top/pdf/{encoded_paren_doi}.pdf', 'Sci-Hub Encoded'

        encoded = quote(doi, safe='')

        # 3. OpenAlex
        try:
            data = metadata(f'https://api.openalex.org/works/https://doi.org/{encoded}')
            prim = data.get('primary_location') or {}
            if prim.get('is_oa') and prim.get('pdf_url'):
                yield prim['pdf_url'], 'OpenAlex Primary'
            for loc in data.get('locations', []):
                if loc.get('is_oa') and loc.get('pdf_url'):
                    yield loc['pdf_url'], 'OpenAlex'
        except Exception:
            pass

        # 4. Unpaywall
        email = get_email()
        if email:
            try:
                data = metadata(f'https://api.unpaywall.org/v2/{encoded}?email={quote(email)}')
                for loc in data.get('oa_locations', []):
                    if loc.get('url_for_pdf'):
                        yield loc['url_for_pdf'], 'Unpaywall'
            except Exception:
                pass

        # 5. Semantic Scholar
        try:
            data = metadata(f'https://api.semanticscholar.org/graph/v1/paper/DOI:{encoded}?fields=openAccessPdf')
            loc = data.get('openAccessPdf') or {}
            if loc.get('url'):
                yield loc['url'], 'Semantic Scholar'
        except Exception:
            pass

        # 6. Europe PMC
        try:
            res = metadata('https://www.ebi.ac.uk/europepmc/webservices/rest/search?format=json&query=' + quote('DOI:"' + doi + '"'))
            records = res.get('resultList', {}).get('result', [])
            for rec in records:
                if rec.get('isOpenAccess') == 'Y' and rec.get('pmcid'):
                    yield f"https://europepmc.org/articles/{rec['pmcid']}?pdf=render", 'Europe PMC'
        except Exception:
            pass


def download_paper_pipeline(ident, title='', engines=None, target_folder=None, custom_name=None):
    """
    Downloads literature with an automatic multi-channel fallback and a hard 5-minute timeout.
    """
    start_time = time.time()
    raw_ident = str(ident or '').strip()
    clean_ident = re.sub(r'^https?://(?:dx\.)?doi\.org/|^doi:\s*', '', raw_ident, flags=re.I)
    result = dict(
        identifier=clean_ident,
        title=title,
        status='FAILED',
        format='-',
        source='未找到可用全文或超出下载时限',
        size_kb=0,
        pages='-',
        filename='-',
        path=None
    )

    folder = Path(target_folder or '.')
    folder.mkdir(parents=True, exist_ok=True)

    if custom_name:
        name = custom_name if custom_name.endswith('.pdf') else custom_name + '.pdf'
    else:
        name = re.sub(r'[^A-Za-z0-9._-]', '_', clean_ident)[:100] + '-' + hashlib.sha256(clean_ident.lower().encode()).hexdigest()[:10] + '.pdf'
    path = folder / name

    def success(content, source):
        if not content.startswith(b'%PDF'):
            raise ValueError('Not PDF')
        pages = len(PdfReader(io.BytesIO(content)).pages)
        if not pages:
            raise ValueError('Empty PDF')
        with tempfile.NamedTemporaryFile(dir=folder, delete=False, suffix='.tmp') as stream:
            stream.write(content)
            tmp = stream.name
        os.replace(tmp, path)
        result.update(
            status='SUCCESS',
            format='PDF',
            source=source,
            pages=pages,
            size_kb=round(len(content) / 1024, 1),
            filename=name,
            path=str(path)
        )
        return result

    # Check local cache first
    if path.exists() and path.stat().st_size > 1000:
        try:
            return success(path.read_bytes(), 'Local Disk')
        except Exception:
            pass

    seen = set()
    try:
        for url, source in candidates(raw_ident):
            # Enforce 5-minute timeout rule: cancel if runtime exceeds 300 seconds
            if time.time() - start_time > MAX_PIPELINE_TIMEOUT:
                result.update(source='下载超时（超过5分钟自动取消）')
                break

            if not url or url in seen or len(seen) >= 15:
                continue
            seen.add(url)
            try:
                data = public_get(url, timeout=(10, 30))
                if data.startswith(b'%PDF'):
                    return success(data, source)
            except Exception:
                continue
    except Exception as e:
        result['source'] = f'下载异常: {str(e)[:40]}'

    if time.time() - start_time > MAX_PIPELINE_TIMEOUT:
        result.update(source='下载超时（超过5分钟自动取消）')

    return result
