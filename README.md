# 崩铁遗器实时计分工具

一个基于 OCR 的《崩坏：星穹铁道》遗器实时评分桌面工具。游戏内打开遗器详情，按热键即可在屏幕顶部 overlay 实时显示期望伤害、副词条评分、替换差值，帮助快速判断遗器取舍。

无需联网、无需上传截图，OCR 全部在本地完成（依赖 [Umi-OCR](https://github.com/hiroi-sora/Umi-OCR)）。

## 特性

- **实时 overlay 显示**：期望伤害、伤害来源拆解、各副词条分数与替换差值，悬浮在屏幕顶部，鼠标可穿透不影响游戏操作
- **错误提示内嵌**：报错信息直接显示在 overlay 底部，不再弹窗打断游戏
- **多角色配置**：可新建/切换/重命名/删除多个角色，每个角色独立保存有效副词条、拐力、转模公式、遗器
- **自定义伤害来源**：支持攻击力 / 生命值 / 防御力 / 速度 / 击破特攻 / 自定义公式
- **转模公式**：用 Python 表达式定义词条转模（如 `ATK * 0.001` 转为暴击伤害），可叠加多条
- **有效副词条多选**：未勾选的词条识别但计 0 分，暴击率/爆伤非有效词条时不参与期望伤害计算
- **撤回机制**：误替换可一键撤回上次更替
- **调试模式**：在 overlay 左侧显示 OCR 解析出的词条名与数值，方便排查识别错误
- **强制管理员模式**：exe 内嵌 UAC 清单，双击自动提权，确保全局热键在游戏以管理员权限运行时仍可用

## 截图

<img width="2189" height="1119" alt="4c450ef9c6d1d1431452d0f5730efa55" src="https://github.com/user-attachments/assets/44bc7803-47fb-4ea9-9dbe-36032eb2af8a" />


## 准备

### 1. 安装 Umi-OCR

下载并启动 [Umi-OCR](https://github.com/hiroi-sora/Umi-OCR)，开启本地 HTTP 服务，默认端口 `1224`。

### 2. 获取本工具

```powershell
git clone https://github.com/<your-name>/<your-repo>.git
cd <your-repo>
```

### 3. 安装依赖（仅源码运行需要）

```powershell
pip install -r requirements.txt
```

## 启动

### 方式 A：直接使用打包好的 exe（推荐）

下载 [Releases](../../releases) 中的 `崩铁遗器评分.exe`，双击运行（会自动弹出 UAC 提权提示）。

### 方式 B：源码运行

```powershell
python hsr_relic_scorer.py
```

或双击 `run.bat`（同样会申请管理员权限）。

> 若游戏以管理员权限运行，本工具也必须以管理员权限运行，否则全局热键可能收不到。

## 使用流程

1. **填写拐力**：在「队友拐力」和「自拐」中填写攻击力、暴击率、暴击伤害等，支持 `100+200` 这种累加格式。
2. **配置截图区域**：
   - `脱装备详情`：角色属性详情页区域（用于记录基础值）
   - `遗器详情`：单件遗器详情区域
   - 格式为 `左上x,左上y | 右下x,右下y`，例如 `100,200 | 660,590`
   - 点击「采样」后，移动到区域左上角按回车，再移动到右下角按回车
3. **预览截图**：点击「截图详情预览」或「截图遗器预览」确认实际截到的画面
4. **记录基础值**：角色全脱装备后打开属性详情，按数字键 `1`
5. **记录遗器**：穿上 6 件遗器，逐件打开遗器详情，按空格记录
6. **替换遗器**：换新遗器时打开对应遗器详情，按空格，工具会替换同位置旧遗器并显示期望伤害差值
7. **撤回**：按退格键撤回上一次更替

### 热键

| 热键   | 功能               |
| ------ | ------------------ |
| `1`    | 记录基础值         |
| `空格` | 记录/替换当前遗器  |
| `退格` | 撤回上次替换       |
| `回车` | 采样模式下采点     |

热键仅在星穹铁道窗口处于前台时生效，避免误触。

## 角色配置

- **有效副词条**：勾选该角色需要计分的副词条，未勾选的词条识别但记 0 分
- **伤害来源**：选择期望伤害计算的基数（攻击力/生命值/防御力/速度/击破特攻/自定义公式）
- **转模公式**：通过 Python 表达式定义词条转模，可叠加多条。可用变量包括 `ATK`、`HP`、`DEF`、`SPD`、`CR`（暴击率）、`CD`（暴击伤害）、`DMG`（属性加伤）等，可用函数 `min`、`max`、`abs`、`sqrt`
- **拐力**：队友拐力与角色自拐分开填写，作用相同（叠加到对应属性）

所有配置按角色独立保存，切换角色时自动加载。

## 配置文件

程序会在 exe / 脚本同级目录生成 `hsr_relic_config.json`，保存：

- 截图区域坐标
- 所有角色配置（有效副词条、拐力、转模公式、伤害来源、基础值、遗器）
- 调试模式开关
- 当前角色

启动时自动读取，记录基础值、记录/替换遗器、撤回、修改输入框或采样坐标后会自动保存。

## 计分规则

- 单条副词条分数 = 该词条对最终期望伤害的占比 `× 100`，再乘角色权重（有效词条权重 1.0，无效词条 0.0）
- 主词条会计入最终伤害
- 替换遗器时，单条差值按词条类型 `(名称, 是否百分比)` 匹配，不按词条行号匹配
- 期望伤害模型：`伤害来源基数 × (1 + 暴击率 × 暴击伤害)`，转模公式会在计算前应用
- 暴击率超过 100% 时，overlay 会额外显示理论值（按未截断暴击率计算）
- 遗器总分和总分差值显示在主词条左侧；副词条分和副词条差值显示在对应副词条左侧

## 项目结构

```
.
├── hsr_relic_scorer.py      # 主程序（单文件）
├── hsr_relic_config.json    # 运行时自动生成的配置文件
├── hsr_relic_scorer.log     # 运行时自动生成的日志文件
├── requirements.txt         # Python 依赖
├── run.bat                  # Windows 启动脚本（自动提权）
└── README.md
```

## 依赖

- [Pillow](https://python-pillow.org/) - 图像处理
- [mss](https://github.com/BoboTiG/python-mss) - 屏幕截图
- [pynput](https://github.com/moses-palmer/pynput) - 全局热键监听
- [requests](https://docs.python-requests.org/) - Umi-OCR HTTP 调用
- [psutil](https://github.com/giampaolo/psutil) - 进程识别（判断游戏窗口）
- [numpy](https://numpy.org/) - 截图滤镜（过滤绿色/暗色像素提升 OCR 精度）
- [Umi-OCR](https://github.com/hiroi-sora/Umi-OCR) - 本地 OCR 服务（需单独安装）

## 从源码打包 exe

```powershell
pip install pyinstaller
pyinstaller --noconfirm --onefile --windowed --uac-admin --name "崩铁遗器评分" ^
  --collect-submodules pynput --collect-submodules mss hsr_relic_scorer.py
```

产物在 `dist\崩铁遗器评分.exe`。`--uac-admin` 会内嵌管理员清单，双击自动提权。

## Windows 注意点

- 程序启动时会启用 Per-Monitor DPI Awareness，截图坐标按物理像素对齐，避免界面缩放导致偏移
- overlay 使用 `WS_EX_LAYERED | WS_EX_TRANSPARENT` 实现鼠标穿透，不会拦截游戏点击
- 如果直接用 `python hsr_relic_scorer.py` 启动且热键无效，请改用 `run.bat` 或打包后的 exe

## License

MIT
