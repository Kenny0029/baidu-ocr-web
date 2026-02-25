# Render 免费云部署（公网可访问）

## 目标
把当前 OCR 网页应用部署到 Render，得到一个公网 URL。  
别人可直接打开这个 URL，上传自己的文件并填写自己的 `API_KEY/SECRET_KEY`。

## 1. 准备代码仓库
1. 在 GitHub 新建仓库（例如 `baidu-ocr-web`）。
2. 把本项目代码推送到该仓库。
3. 不要上传 `key.txt`（已在 `.gitignore` 中排除）。

## 2. 在 Render 部署
1. 登录 Render 控制台。
2. 选择 `New` -> `Blueprint`（推荐，自动读取 `render.yaml`）。
3. 选择你的 GitHub 仓库，点击部署。
4. 等待构建完成，Render 会给你一个公网地址，例如：
   `https://baidu-ocr-web.onrender.com`

## 3. 交付给其他人使用
把 Render 的公网地址发给他人即可。  
他们打开网页后，直接：
1. 上传 PDF 或图片文件夹
2. 输入自己的 `API_KEY` / `SECRET_KEY`
3. 点击开始识别并下载 CSV

## 4. 免费层说明
- 免费实例有空闲休眠，首次访问可能有冷启动等待。
- 不同时间 Render 政策可能调整，建议在控制台确认当前免费额度与限制。

## 5. 常见问题
- 构建失败：查看 Render 的 Build Logs。
- OCR 请求失败：通常是用户输入的 Key 不正确，或百度接口临时不可达。
- 上传过大失败：压缩文件体积或拆分处理（应用默认限制约 100MB）。
