# 来源与授权说明

## 上游
本仓库是 [`yuz_cn/wechat-notify`](https://gitee.com/yuz_cn/wechat-notify)（下称"原项目"）的个人修改版，
基于其 **v4.6.1** 的源码。

- 原项目作者：YikZ（Gitee: <https://gitee.com/yuz_cn>）
- 原项目地址：<https://gitee.com/yuz_cn/wechat-notify>

## 本仓库做了什么
只在「横幅显示位置」上增加自定义坐标（`banner_pos_x` / `banner_pos_y`）能力，
其余文件（`wechat_db.py`、`wecom_notify.py`、`wx_key_engine.py`、`sounds/`）均为原项目内容，未作修改。
逐处改动见 README 第 4 节。

## 授权状态
原项目仓库**未声明开源许可证（LICENSE）**，Gitee 仓库页面的提示为：
"该仓库未声明开源许可证文件（LICENSE），使用请关注具体项目描述及其代码上游依赖"。

因此：

- 本仓库**不附加**任何许可证，也不主张额外授权；
- 仅作为个人学习、自用修改的记录；
- 如需二次分发、商用或打包发行，请先联系原项目作者取得许可；
- 若原作者对本仓库有异议，请联系删除。

## 数据与隐私
- 本仓库不包含任何使用者本机的配置、微信数据库、密钥、日志或截图中的个人信息。
- 程序运行时读取微信本地数据库（`db_storage\message\message_*.db-wal`）属于原项目行为，
  数据只在本机处理，不上传。
