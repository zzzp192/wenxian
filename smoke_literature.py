"""Real network download smoke test; uses a private temporary library."""
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import zipfile

from fastapi.testclient import TestClient
from pypdf import PdfReader

with tempfile.TemporaryDirectory(prefix='efmat-smoke-') as tmp:
    os.environ['LITERATURE_DATA_DIR'] = tmp
    sys.path.insert(0, str(Path(__file__).resolve().parent / 'literature'))
    from app import app
    client = TestClient(app)
    result = client.post('/api/download/single', json={'identifier': '10.48550/arXiv.1706.03762', 'engines': ['oa']}).json()
    assert result['status'] == 'SUCCESS', result
    pdf = client.get('/api/files/download/downloads/' + result['filename'])
    reader = PdfReader(io.BytesIO(pdf.content))
    assert len(reader.pages) > 0
    assert 'Attention' in (reader.pages[0].extract_text() or '')
    batch = client.post('/api/download/batch/text', json={'text': '10.48550/arXiv.1706.03762\ninvalid-doi', 'workers': 2}).json()
    task = client.get('/api/tasks/' + batch['task_id']).json()
    assert (task['status'], task['success'], task['failed']) == ('COMPLETED', 1, 1), task
    library = client.get('/api/library').json()
    assert library['count'] == 1, library
    package = client.post('/api/package/zip').json()
    archive = client.get('/api/files/zip')
    with zipfile.ZipFile(io.BytesIO(archive.content)) as z:
        assert z.testzip() is None
        assert result['filename'] in z.namelist()
    print(json.dumps({'single': result, 'pdf_title_verified': True, 'batch': {'success': task['success'], 'failed': task['failed']}, 'library_count': library['count'], 'zip_files': package['total_files']}, ensure_ascii=False))
