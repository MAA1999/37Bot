# 37Bot

基于 [NcatBot](https://github.com/liyihao1110/NcatBot) 的 QQ 机器人。

## 快速开始

### 1. 安装依赖

```bash
uv sync
```

### 2. 配置

```bash
cp config.yaml.example config.yaml
```

编辑 `config.yaml`：

```yaml
root: '管理员QQ号'
bt_uin: '机器人QQ号'
napcat:
  ws_uri: ws://localhost:3001   # NapCat WebSocket 地址
  remote_mode: true             # 远程模式
```

### 3. 运行

```bash
uv run python main.py
```

## 插件与命令

### 通用

- `/help`：显示模块列表
- `/help <模块>`：显示模块命令
- `/status`：查询服务器状态（CPU、内存、Swap、磁盘、运行时间）

### ArkRec（明日方舟少人 Wiki）

- `/arkrec [关卡] [分类] [干员]`：查询记录（默认常规队当前纪录）
- `/arkrec_top [数量] [分类]`：查看最近记录
- `/arkrec_op <关卡号>`：查看关卡信息
- `/arkrec_exclusive [干员] [流派] [普通/突袭] [数量]`：查询独享纪录（图片）
  - 默认不含已关闭活动，可加 `已关闭`
- `/arkrec_brief [流派] [数量]`：关卡一览（图片）
  - 默认：当前活动 + 常规队 + 含无纪录关卡
  - 可加：`有记录`、`仅无记录`、`全部关卡`、`刷新`
- `/arkrec_sub <分类/干员/关卡>`：\[管理员] 订阅推送
- `/arkrec_unsub [值]`：\[管理员] 取消订阅（留空取消全部）
- `/arkrec_block <流派>`：\[管理员] 排除流派（不推送该流派记录，无包含条件时推送其余全部）
- `/arkrec_unblock [流派]`：\[管理员] 取消排除流派（留空取消全部排除）
- `/arkrec_status`：查看订阅状态和数据库统计
- `/arkrec_config <email> <password>`：\[root，私聊] 配置账号

### 森空岛签到

- `/skland_config add <鹰角token>`：\[私聊] 添加森空岛签到账号
  - 添加时会立即校验 token、读取绑定角色，并优先使用第一个明日方舟角色 `uid` 作为本地账号标识
  - 添加成功后会立刻尝试签到，并返回本次签到结果
- `/skland_sms <手机号>`：发送短信验证码；群内发起时，该账号后续通知目标为当前群
- `/skland_sms_code <验证码>`：提交短信验证码，登录成功后保存长期 token 并立即签到
- `/skland_qr`：二维码登录入口；当前暂不落库，详见命令提示
- `/skland_sign [uid]`：立即签到；不传 `uid` 时签到全部账号
- `/skland_status`：\[root] 查看签到配置、账号 UID、上次定时日期
- `/skland_config remove <uid>`：\[root/账号添加者] 删除账号
- `/skland_config hour <0-23>`：\[root] 设置每日定时签到小时，分钟固定为 `01`
- `/skland_config on|off`：\[root] 启用或禁用每日自动签到

说明：

- 默认每日本机时区 `00:01` 对所有账号自动签到，当前会同时尝试明日方舟和终末地。
- token 添加只允许私聊，签到结果会私聊添加者；短信登录可在群或私聊完成，签到结果会发回发起来源。
- token、设备 ID、所属 QQ 用户等数据保存在插件工作目录的 `config.json`，不会写入主配置文件。
- token 失效或换取森空岛 `cred` 失败时，只会在首次失败时提醒一次：群来源会在对应群内 at 添加者，私聊来源会私聊添加者；后续签到恢复成功会自动清除提醒状态。
- 日志会记录 `uid`、所属 QQ、token 指纹、设备 ID、接口阶段、角色签到结果和耗时，便于排查；不会记录明文 token。
- 森空岛 API 签名算法参考了 [YueHen14/skyland-auto-sign](https://github.com/YueHen14/skyland-auto-sign) 和 [UKMeng/nonebot-plugin-skland-arksign](https://github.com/UKMeng/nonebot-plugin-skland-arksign)。

### Mirror酱

- `/mirror_sub <rid> [类型] [渠道] [间隔秒] [自动上传]`：\[管理员] 订阅资源更新
- `/mirror_unsub <rid> [类型]`：\[管理员] 取消订阅
- `/mirror_list`：查看本群订阅
- `/mirror_check [rid] [force]`：\[管理员] 立即检查更新（force 强制显示更新信息）
- `/mirror_config <rid> [类型] [间隔秒] [自动上传] [渠道]`：\[管理员] 修改订阅配置
- `/mirror_download <rid> [类型] [渠道]`：\[管理员] 下载最新版并上传群文件
- `/mirror_cdk <cdk>`：\[root，私聊] 设置 CDK

说明：

- 类型即平台包：0通用/1win-x64/2android-any/3android-arm64/4android-x64/5macos-arm64/6macos-x64/7linux-x64/8linux-arm64/9win-arm64。
- 渠道 stable/beta/alpha；检查间隔至少 60 秒，默认 600 秒。
- 开启自动上传后，新版本自动下载并上传到群文件「{rid}下载」文件夹，文件名形如 `M9A-win-x64-v4.11.2.zip`、`MaaEnd_exec-android-any-v2.32.0-beta.4.APK`（安卓包后缀大写绕过 QQ 拦截）。
- 未设置 CDK 时只发更新通知，不自动上传。

### 群管

- `/ga_enable`：\[管理员] 启用本群群管功能
- `/ga_disable`：\[管理员] 禁用本群群管功能
- `/ga_pattern <正则>`：\[管理员] 设置入群验证正则
- `/ga_reject <启用> <理由>`：\[管理员] 设置自动拒绝
- `/ga_status`：查看本群群管状态
- `/ga_query [QQ号]`：\[管理员] 查询成员记录

### 待办

- `/todo_add <内容>`：添加待办（支持回复消息）
- `/todo_list`：查看待办列表
- `/todo_done <id>`：完成待办

## 项目结构

```plaintext
37Bot/
├── main.py
├── config.yaml
├── config.yaml.example
├── plugins/
│   ├── arkrec/
│   ├── groupadmin/
│   ├── help/
│   ├── mirrorchyan/
│   ├── skland/
│   ├── status/
│   └── todo/
├── 37bot.service
└── start-napcat.sh
```

## License

[GPL-3.0](LICENSE)
