# assets 目录

可视化 demo 的本地静态素材：Pico CSS 2.0.6、Lucide 0.462.0 图标、图标 sprite 与项目主题。

- 第三方（只读，勿改）：`pico.min.css` 与 10 个 Lucide `*.svg`，来源与哈希见 `LICENSES.md`。
- 项目自有（可改）：`theme.css`、`icons.svg`。
- 升级步骤：改 `LICENSES.md` 中的上游 URL → 重新下载同名文件 → 重算 SHA-256 → 同步 `LICENSES.md` 表格。
- 全部文件离线本地化，运行时不访问网络；新增素材须在此目录登记并附哈希。
