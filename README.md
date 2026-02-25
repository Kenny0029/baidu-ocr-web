# Baidu OCR Web

[![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy?repo=https://github.com/Kenny0029/baidu-ocr-web)

Web UI for Baidu OCR:
- Upload PDF or image folder
- Input API_KEY and SECRET_KEY
- Track progress
- Download CSV result

## One-click Cloud Deploy (Render)
1. Click the **Deploy to Render** button above.
2. Connect your GitHub account if asked.
3. Confirm deployment settings and create service.
4. Wait for build to finish, then open the generated URL.

## Local Run
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python web_app.py
```



Thanks to Yuqi CHen
