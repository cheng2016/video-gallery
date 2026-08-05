# Release checklist

## 打标签发布（推荐）

```bat
git tag v1.x.y
git push origin v1.x.y
```

GitHub Actions 会在 Windows 上跑 PyInstaller，上传 `VideoGallery-v1.x.y-windows-x64.zip` 到 Release。

## 本地打包

```bat
build.bat
```

把 `dist\VideoGallery\` 打成 zip 即可手动挂到 Release。

## Release 正文建议模板

```markdown
## 本地视频库 vX.Y.Z

扫盘 → 封面墙 → 点播。解压后双击 Start-VideoGallery.bat。

推荐：winget install ffmpeg

搜索：拼音 / ext:mkv / genre:动作 / actor:姓名
```

把 `docs/images/demo.gif` 与三张步骤图贴进 Release 说明可显著提高下载转化。
