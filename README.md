# 本地视频库

扫描本地硬盘视频，按文件夹/类型分类，生成加密预览图，浏览器浏览播放。

## 快速开始

1. 安装 [Python 3.10+](https://www.python.org/downloads/)（勾选 Add to PATH）
2. （推荐）安装 ffmpeg：`winget install ffmpeg`
3. 双击 **`start.bat`**
4. 浏览器打开 http://127.0.0.1:8765

## 功能

- 盘符选择：打开此盘（读缓存秒开）/ 重新扫描（全盘）
- 左侧频道：一级文件夹（建议命名：电影、电视剧、动漫…）
- 顶部筛选：排序、类型（动作/恐怖/爱情…有片才显示）、格式
- 预览图：保存在程序根目录 `preview_cache\`，文件加密（`.vgt`）
- 侧栏可隐藏

## 缓存说明

| 操作 | 行为 |
|------|------|
| 重启 / 打开此盘 | 优先读 `preview_cache`，不重复截图 |
| 重新扫描 | 全盘重扫；已有预览图仍复用 |
| 删了的视频 | 加载缓存时自动忽略 |

预览图目录：`D:\video-gallery\preview_cache\`（与 `app.py` 同级）

## 类型识别

从路径/文件名匹配，例如：

```text
电影\动作\xxx.mp4
恐怖片\某某.mkv
```

没有匹配到的类型不会出现在筛选栏。

## 命令行

```bat
start.bat
start.bat E:\电影
.venv\Scripts\python.exe app.py "E:\电影" --port 8765
.venv\Scripts\python.exe app.py --rescan
```

## 支持格式

mp4 / mkv / avi / mov / wmv / flv / webm / m4v / ts 等

部分 mkv/avi 浏览器可能无法播放，建议 Chrome 或转成 H.264 mp4。
