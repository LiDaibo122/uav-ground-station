# 第三方源码与许可

本仓库跟踪 `SOURCES.json`、此说明和 `patches/`，不重复上传第三方 Git 检出目录及编译产物。

首次克隆地面站后，在根目录执行：

```bash
python3 -m pip install -r requirements-simulation.txt
python3 scripts/fetch_auction_sources.py --build-baseline
```

脚本按清单检出 CBBA-Python 和 SSlap 的确定提交，应用记录在本仓库的 NumPy 兼容补丁，并编译可选基础拍卖实现。第三方目录原有 `.git` 和本机编译结果保持不变。

- CBBA-Python：MIT，原始许可随检出源码保存在 `cbba_python/LICENSE`。
- SSlap：MIT，原始许可随检出源码保存在 `sslap/LICENSE.txt`。
- 网页中文字体许可保存在 `web/fonts/LICENSE.txt`，该字体及许可随本仓库提交。

本次整理未为自研代码选择新的开源许可证；上传到 GitHub 不代表自动授予开源许可。
