# 37Bot

基于 [NcatBot](https://github.com/liyihao1110/NcatBot) 的 QQ 机器人。

## 快速开始

### 1. 安装依赖

```bash
uv sync
```

### 2. 配置

复制配置模板并填写：

```bash
cp config.yaml.example config.yaml
```

编辑 `config.yaml`：

```yaml
root: '管理员QQ号'
bt_uin: '机器人QQ号'
napcat:
  ws_uri: ws://localhost:3001      # NapCat WebSocket 地址
  remote_mode: true                 # 远程模式
```

### 3. 运行

```bash
python main.py
```

## 项目结构

```plaintext
37Bot/
├── main.py              # 主程序入口
├── config.yaml          # 配置文件 (git ignored)
├── config.yaml.example  # 配置模板
├── plugins/             # 插件目录
│   ├── status.py        # 服务器状态查询
│   └── help.py          # 帮助命令
├── 37bot.service        # systemd 服务配置
└── start-napcat.sh      # NapCat Docker 启动脚本
```

## 内置功能

- `ping` - 回复 pong
- `/hello` - 问候
- `/help` - 帮助信息

## License

[GPL-3.0](LICENSE)
