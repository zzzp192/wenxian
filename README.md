# EFMAT 文献全自动聚合下载工具 (Literature Downloader)

轻量级、多渠道、高容错的学术文献与专著全自动聚合下载工具。

本项目由部署于生产环境服务器的 EFMAT 文献模块移植增强而来，打通了开源开放获取（OA）、权威预印本、学术知识库、专利库以及多重镜像检索链路。

---

## 一、核心特性与技术亮点

1. **多级自动降级检索链路**：
   - **OA 开放获取**：集成 Unpaywall、Crossref、arXiv、CORE、Europe PMC、Semantic Scholar 等开放接口。
   - **学术镜像增强**：内置 Sci-Hub 自动解析通道，特别支持含括号特殊 DOI（如 `(00)12345-X`）的双重 URL 编码自动纠错。
   - **权威机构与专利知识库**：支持英国国家物理实验室（NPL ePrints）报告及 Google Patents 官方专利直链自动解析。
   - **大部头专著与镜像 CDN**：支持 LibGen 动态 CDN 解析及分章节/大文件断点续传。
2. **高稳定性与超时熔断**：
   - 单篇下载设置 **300 秒（5分钟）全局硬超时**，杜绝冷门文献或卡死站点无限挂起。
   - 包含流式下载与连接断开自动重试、防假死机制。
3. **PDF 页面与格式自验**：
   - 下载完成后自动进行 PDF 结构完整性与非空校验，自动过滤反爬拦截 HTML 假文件。
4. **简洁易用的 Web UI**：
   - 基于 FastAPI + Tailwind/Lucide 构建，支持单篇查询下载、批量导入（文本/Excel/CSV）、实时进度展示、本地文献库管理与一键打包 ZIP。

---

## 二、目录结构

```text
├── literature/
│   ├── app.py                  # FastAPI Web 后端服务主程序
│   ├── oa_engine.py            # 核心多渠道文献下载引擎（超时、重试、解析）
│   ├── templates/
│   │   └── index.html          # Web 前端主界面
│   └── static/
│       ├── input.css           # 基础样式定义
│       ├── styles.css          # 构建后样式表
│       └── lucide.js           # 图标库
├── requirements.txt            # Python 依赖项清单
├── smoke_literature.py         # 自动化冒烟测试套件
├── 启动文献下载器.cmd          # Windows 一键启动脚本
├── .gitignore                  # Git 忽略配置（忽略大文件 PDF 与临时数据）
└── README.md                   # 项目使用与配置说明
```

---

## 三、快速开始

### 1. 环境准备
确保已安装 Python 3.9+。进入项目根目录安装所需依赖：
```bash
pip install -r requirements.txt
```

### 2. 启动服务
- **Windows 用户**：直接双击根目录下的 `启动文献下载器.cmd`。
- **命令行启动**：
  ```bash
  cd literature
  python app.py
  ```

服务默认运行在：`http://127.0.0.1:7860`

### 3. 使用方式
1. 打开浏览器访问 `http://127.0.0.1:7860`；
2. **单篇下载**：输入 DOI（如 `10.1016/j.ijrmhm.2014.04.010`）、arXiv ID（如 `1706.03762`）或 PubMed PMID，点击即可快速解析并自动下载；
3. **批量下载**：支持多行输入或上传包含文献标识符的 Excel / CSV 表格；
4. **文献库管理**：在“文献库”标签页中预览已下载文献，或点击一键打包下载为 ZIP 压缩包。

---

## 四、自动化冒烟测试
在项目根目录下执行以下命令，验证核心下载链路与 API 正常：
```bash
python smoke_literature.py
```
全部通过时将打印自检成功的 JSON 结果并返回退出码 0。
