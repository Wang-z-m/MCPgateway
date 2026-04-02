# MCP 网关实验报告

- 生成时间：`2026-03-26T04:43:01.724931+00:00`
- 功能验证成功数：`4`
- 功能验证失败数：`2`
- 并发场景数：`3`
- 最低并发成功率：`1.0`
- 最大 P95 时延：`603ms`

## 功能验证
- `get_user`: PASS, latency=55ms, attempts=1
- `create_order`: PASS, latency=54ms, attempts=1
- `probe_slow`: PASS, latency=374ms, attempts=1
- `probe_failure`: FAIL, latency=65ms, error_code=-32050, category=DOWNSTREAM_ERROR
- `probe_retry`: PASS, latency=71ms, attempts=2
- `validation_error`: FAIL, latency=25ms, error_code=-32602, category=VALIDATION_ERROR

## 并发验证
- 并发 `5` x 轮次 `1`: success_rate=1.0, avg=103.2ms, p95=132ms
- 并发 `10` x 轮次 `1`: success_rate=1.0, avg=184.1ms, p95=244ms
- 并发 `20` x 轮次 `1`: success_rate=1.0, avg=404.65ms, p95=603ms

## 热更新验证
- 新增工具 `probe_reload_temp`，reload changes={"added": ["probe_reload_temp"], "removed": [], "updated": [], "unchanged_count": 6}