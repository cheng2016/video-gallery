# 视频数据契约

项目功能之间只应共享视频条目，不应直接依赖扫描、清理或转码的内部实现。
契约代码位于 `vg/schema.py`。

## 磁盘格式

每个片库根目录对应一份 `index.json`：

```json
{
  "schema_ver": 2,
  "root": "D:\\Videos",
  "videos": [],
  "updated": "2026-08-06T10:00:00"
}
```

所有写入必须经过 `vg.schema.serialize_video_item()`；禁止功能模块自行筛字段后写
`index.json`。未知字段会保留，便于将来扩展。

## 稳定字段

- 身份：`id`
- 文件：`name`、`filename`、`rel`、`folder`、`ext`
- 属性：`size`、`size_h`、`mtime`、`mtime_h`、`duration`、`duration_h`
- 归属：`root`、`_lib_root`、`_lib_cache`、`_folder_raw`
- 类型：`kind`、`segments`、`seg_count`
- 元数据：`genres`、`probe_ver`、`probe_duration_done`、`probe_audio_done`、`audio_codec`、`audio_hard`
- 状态：`thumb`、`has_thumb`、`thumb_v`、`bad`、`bad_reason`

`id` 在多盘合并时可能为避免冲突而临时改写；原磁盘 ID 存在运行时字段
`_thumb_id`。落盘时序列化器会自动恢复原 ID。

## 运行时字段

`RUNTIME_ONLY_FIELDS` 是唯一清单，包含：

- 搜索缓存：`_q`
- 多盘显示/别名：`_thumb_id`、`_lib_label`、`lib_label`
- 重复标记：`dup`、`dup_n`、`dup_reason`
- 剧集派生：`series_id`、`series_title`、`series_n`、`cover_id`、`is_series`、`episodes`
- API 缩略图别名：`thumb_id`

这些字段可以由功能模块派生，但绝不能写入 `index.json`。

## 重复检测

唯一实现位于 `vg/duplicates.py`：

- `find_duplicate_groups()`：纯函数，供清理界面使用；
- `mark_duplicates()`：使用同一分组结果，只负责写入运行时角标。

规则仍是同名，或大小完全相同且不小于 `MIN_VIDEO_FILE_BYTES`。修改重复规则时
只改这个模块，并同时运行 `tests/test_duplicates.py` 与
`tests/test_cleanup_scope.py`。

## 模块边界

- `vg/catalog.py`：频道、搜索串、目录树和派生索引。只有
  `apply_catalog_to_state()` 可以把完整 Catalog 写回 `STATE`。
- `vg/scan.py`：只负责扫描流程、缩略图批任务和扫描完成通知；为兼容旧调用，
  暂时重导出部分 `vg.catalog` 函数。
- `vg/roots.py`：只负责多盘挂载与合并，不再为树/频道加载扫描模块。
- `vg/cleanup.py`：清理范围、坏文件和响应编排；重复规则仍只在
  `vg/duplicates.py`。
- `vg/catalog_repository.py`：片库读取边界。功能依赖 `VideoLookup`、
  `CatalogScopeReader`、`MountedRootsReader` 等窄协议，不直接了解
  `STATE + disk_libs + roots` 的查找顺序。
- `vg/routes/`：低耦合 HTTP 适配层。路由只解析请求并调用功能模块，不直接从
  `vg.web` 导入全局 `app`。
- `vg/web.py`：保留 Flask `app`、浏览/播放/删除等尚未迁移的高风险路由，并
  通过 `register_feature_routes(app)` 注册独立路由。

新增功能优先放入独立服务，再由 `vg/routes/` 注册；不要把业务规则重新写回
`web.py`，也不要让 `roots.py` 直接依赖 `scan.py` 的内部实现。

## 设计原则落地

- 单一职责：schema、catalog、repository、cleanup、routes 分别只负责契约、
  派生计算、读取适配、清理编排和 HTTP。
- 开闭原则：新增叶子 API 通过新的 `register(app)` 模块扩展，核心 `web.py`
  不再增加对应业务实现。
- 里氏替换：服务只依赖 Protocol；测试中的 Fake Repository 可替换运行时实现。
- 接口隔离：只查视频的功能依赖 `VideoLookup`，只做范围清理的功能依赖
  `CatalogScopeReader`，不强迫使用完整仓库。
- 依赖倒置：cleanup 与 convert 路由依赖上述协议，由组合根注入默认适配器。
- 迪米特法则：功能不再跨层遍历 `STATE`、`disk_libs` 和多盘回退细节，这些
  细节封装在 `RuntimeCatalogRepository` 中。
