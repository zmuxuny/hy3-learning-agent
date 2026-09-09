# Protocol 1.16 离线回归

从 Protocol 1.13 regression 原 CaseSpec 与资源逐字节复用，重新绑定活动 Protocol 1.16。engineering 用于运行隔离、动作分类、失败终态与发布治理回归；calibration 用于活动 Judge 合同连通性检查。均为已见、受控响应，不进入 E7 新测试或能力统计。

E7 新测试见 `../e7-test-e7-j01` 至 `../e7-test-e7-j04`；同轨迹真实 Judge 比较使用 E6 1.15 原轨迹和 `../e7-method-controls`，不会用本目录固定响应替代真实调用。
