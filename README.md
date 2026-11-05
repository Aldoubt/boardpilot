# BoardPilot · 板载助手

面向 Linux 板载设备的可视化 SSH 开发与部署助手。

用图形界面完成源码打包、SSH 连接、校验与断点上传、暂存解压、安装预览、源码备份、板载编译和日志查看。开发电脑可以是 Windows 或 Ubuntu；源码在目标设备自己的环境中编译。

> 当前版本提供 **Sunray / ROS1 Noetic 预设**。工作区要求 `build.sh`，安装复用工作区中的 `tools/workspace_transfer/apply_workspace.sh`。它还不是任意项目都可直接使用的通用部署器；通用 SSH 和传输功能可以独立使用。

## 桌面版下载（预览版）

在 [Releases](https://github.com/Aldoubt/boardpilot/releases) 下载：

- **Windows x64**：运行 `BoardPilot-0.1.1-windows-x64.exe`，不需要安装 Python。首次打开自解压需要几秒钟。
- **Ubuntu 22.04 / 24.04 x86_64**：解压 `BoardPilot-0.1.1-linux-x86_64.tar.gz`，在解压目录运行 `bash install.sh`，然后从应用菜单打开 BoardPilot；也可以直接运行其中的 `./BoardPilot`。需要图形桌面以及 `python3`（安装菜单入口时使用），应用运行不需要自行安装 Python 依赖。源码打包还需要 `git`。
- Linux 安装只写入当前用户目录，不需要 sudo。卸载运行 `bash ~/.local/share/boardpilot/uninstall.sh`；配置和主机指纹会保留。
- Linux 安装支持 `XDG_DATA_HOME`。ARM64/NX 本机桌面包暂未提供；NX 可作为 SSH 连接的目标。
- 下载旁边的 `.sha256` 可用于核对文件完整性。当前预览版未做 Windows 代码签名。

## 从源码启动

需要 Python 3.8+、Git，以及可访问目标设备的网络。

**Windows**：克隆或下载仓库后，双击 `start_gui.bat`。首次会创建 `.venv`，缺少 Paramiko 时自动安装依赖。

**Ubuntu**：

```bash
sudo apt install python3-tk python3-venv git
bash start_gui.sh
```

也可使用已有环境：

```bash
python -m pip install -r requirements.txt
python deployer_gui.py
```

右上角可选择 100%、125%、150% 缩放，默认 125%，选择后自动保存。

## 0.1.1 部署边界修复

- 在预览与应用前检查 Sunray 安装器安全版本。要求安装脚本包含 `readonly SUNRAY_APPLY_SAFETY_VERSION=1`；旧脚本可能误删子模块 `.git`，会被拒绝执行。可将仓库附带的 [安全版安装脚本](presets/sunray/apply_workspace.sh) 复制到工作区内 `tools/workspace_transfer/apply_workspace.sh`，再重新打包。这个文件只负责源码搬运，不修改定位或控制算法。
- 安全版 Sunray 安装器应保留所有层级 Git 元数据，并在包缺少目标已有的已声明子模块源码时拒绝覆盖。仅选择两个编译模块不会缩小源码安装范围。
- 源码备份覆盖子模块未跟踪源码和已物化的非 Git 子目录；同时考虑目标与待安装源码的忽略规则，避免漏备份待删除文件。备份仍不包含构建产物或 Git 元数据。
- 精确识别 ROS Noetic 的 `CMakeLists.txt` 顶层链接并转换为普通 Catkin 入口，转换记录写入 manifest；其他外部链接继续拒绝。

## 使用流程

1. 选择 Sunray Git 工作区并检查分支、提交和未提交修改。
2. 在源码目录之外选择输出位置，生成源码包。包含已检出的子模块源码和配置，排除构建产物、Git 元数据与被忽略文件；记录来源提交及修改清单，并生成 SHA256。
3. 填写设备 IP、SSH 端口、用户名，以及密码或私钥。首次连接核对主机指纹；已知主机公钥变化时拒绝静默覆盖。
4. 上传到 `~/boardpilot_incoming`。中断后选择同一个包再次上传可以续传；传完核对校验值，再解压到独立暂存目录。
5. 选择目标工作区，预览复制与删除计划。预览不停止服务，也不覆盖目标源码。
6. 确认计划后应用。工具先备份目标源码（不含编译产物和 Git 元数据），再运行包内安装脚本。它会覆盖同名源码并删除包内没有的未忽略源码；相关服务停止后不会自动启动。
7. 选择模块及并行数，在设备上通过 `build.sh` 编译。例如修改 Livox 驱动与 EKF 时选择 `livox_ros_driver2 ekf_odometry`。修改接口时还需编译相关依赖模块。
8. 检查结果后，按设备自身流程启动模块与验证。工具不会自动解锁、起飞或发送运动指令。

## 断线、密码与恢复

- 上传中断保留 `.part`，续传前检查首尾已有数据，上传后核对完整 SHA256。重新打包会使用新的包名。
- 编译通过 `nohup` 在目标设备后台运行。SSH 断开或“停止监看”不会终止编译；重新连接后可以查看上次编译任务。
- 安装不是原子切换。安装中断后，服务可能仍停止且源码可能部分更新；应检查备份与日志，再重新预览和应用。
- 源码备份不等于二进制回滚。恢复源码后需要在设备上重新编译；不自动回滚系统依赖、CUDA 或 JetPack。
- 密码仅存在当前进程内，不写入配置。连接配置和上次任务记录保存在 Windows `%APPDATA%/BoardPilot` 或 Linux `~/.config/BoardPilot`，与项目仓库分离。
- 交互终端支持发送命令和 Ctrl+C，但不是完整终端模拟器。出现远端密码提示后可勾选“隐藏输入”再发送密码。
- 应用源码不更改目标 Git HEAD。包内 `.boardpilot-manifest.json` 记录实际来源版本，不要仅凭目标目录的旧 HEAD 判断部署版本。

## 测试

```bash
python -m unittest -v test_deployer
python deployer_gui.py --smoke-test
```

覆盖打包规则、未提交修改、Shell 换行和执行权限、Git worktree、取消操作、解压路径保护、校验、命令引用与上传中断续传。测试使用临时 Git 仓库和模拟 SFTP，不会连接或覆盖真实设备。

## 后续方向

- 可配置项目预设，解除安装脚本、ROS 和工作区名称的绑定。
- 设备连接配置管理、远程日志下载、部署历史。
- Windows 可执行安装包与 Linux 发布包。

当前仓库只包含助手自身代码，不包含无人机业务源码、飞行日志、私钥或个人设备配置。

## 构建桌面包

在目标操作系统运行（不能用 Windows 直接生成 Linux 二进制）：

```bash
python -m pip install -r requirements-build.txt
python packaging/build.py
```

输出位于 `dist/`。GitHub Actions 在 Windows 和 Ubuntu 22.04 分别构建、运行打包后 GUI 冒烟检查，Linux 还检查桌面入口安装与卸载；均通过后发布预览版本。修改 `VERSION` 后推送 main 可发布新版本。
