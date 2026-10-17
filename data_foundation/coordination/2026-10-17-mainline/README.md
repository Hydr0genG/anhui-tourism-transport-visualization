# 主线接收补充：给D应用的补丁与C参考调用

基线D：24eec2d33d6186bacdaafe08e33808c28dcdeb5e。C0.2.1a2/E0.6.0。完整结论见根目录《致D和E-主线实测与30号修复补丁-2026-10-17.md》。

## D先处理30号

`D-30-original-jobs.py`是该提交原始文件的测试输入，不应覆盖D当前源码。应在D仓库检查并应用`D-30-job-readback.patch`：同时修复C datetime转换和缺省analysis_id。`test_D_job_datetime_patch.py`会在临时目录对原文件实际应用补丁，再验证7种时间输入；不导入D应用或连接数据库。

```bash
python -m pytest data_foundation/coordination/2026-10-17-mainline/test_D_job_datetime_patch.py -q
```

## 再接Worker参考

`data_foundation/examples/c_worker_reference.py`是独立示例，不是安装包新增API。D可把调用顺序融入自己的Worker；传入C DataStore和当前JobLease，保留D自有授权、队列和持续心跳。三个成功分支都在C内完成作业，返回`_job_already_completed=True`供现有外层跳过重复完成。

视频参考检查实际counter与已登记模型溯源，C核验输入，再调E处理；C原件路径仅传给内部Worker。参考异常不吞掉，失败状态由D统一选择一个入口完成。27只从C取结果，不再从D私表作为新结果的正式来源。

```bash
python -m pytest data_foundation/tests/test_worker_reference.py -q
```

本测试需E0.6.0及numpy；使用明确标识的合成counter/帧，不能代替真实权重、素材授权或模型精度。纯C环境缺E时模块会明确skip。

## 复跑29/30与19的HTTP主线

将本补充包按原目录合并到既有C完整交接包根目录；保留旧wheel。安装C0.2.1a2/E0.6.0及D pyproject声明的FastAPI、httpx、argon2-cffi、pydantic-settings、python-multipart。实际本轮运行版本见runtime-versions.json。

通过当前进程环境`C_MAINLINE_DATABASE_URL`指定一个**全新空的cde_readiness_*_test MySQL库**，不要使用业务库。输出目录必须尚不存在。然后运行：

```bash
python -I data_foundation/examples/verify_cde_mainline_http.py \
  --d-root /path/to/D-checkout \
  --d-commit <本次完整提交SHA> \
  --output /path/to/new-acceptance-output
```

脚本不自动创建数据库、不清理已有库；会在指定空测试库迁移、导入C既有83条观测、配置一行真实CSV/PDF、创建明确标识的测试账号，使用真实登录/CSRF和ASGI路由完成17项验证。默认不要求数据库禁网络；C本轮运行使用无网络临时实例，可追加`--require-socket-only-instance`加强检查。它不会启动API监听端口。

`--candidate-patch`只用于说明一次性候选副本所应用的补丁摘要；D已提交修复后，用新提交SHA直接复跑，不标候选。输出不包含口令、cookie、CSRF或lease token；测试DATA_ROOT位于新输出目录raw，生产库与原件不改动。

原D失败报告、候选修复报告分别保留在acceptance/C-mainline-HTTP-2026-10-17。D尚未应用/推送本补丁，C候选17/17不能提前记成接收方通过。
