```markdown
# Crypto-Quant-Core 🚀

<!-- 顶部导航栏 -->
[English](#-english) | [中文说明](#-中文说明)

---

<a name="-english"></a>
## 🇬🇧 English

**Crypto-Quant-Core** is an advanced Python-based quantitative trading system designed for cryptocurrency markets (specifically optimized for **OKX**).

This project stands out by integrating a **Dual-AI Engine (Claude & DeepSeek)** to empower trading decisions. It combines traditional quantitative factors with the reasoning capabilities of Large Language Models (LLMs) to achieve smarter execution and analysis.

> ⚠️ **Disclaimer**
> This repository is an **archived version** of a personal trading system. It is provided for educational and research purposes only.
> *   **Performance**: Internal testing has shown promising results, but the market is unpredictable.
> *   **Risk**: Do Your Own Research (DYOR) before running this on a live account.

### ✨ Key Features

*   **🧠 Dual-AI Engine (`claude_reviewer.py`)**:
    *   **Claude & DeepSeek Integration**: Leverages the strengths of both models. DeepSeek handles logic/reasoning, while Claude provides nuanced market analysis.
    *   **Intelligent Review**: Analyzes trading history and summarizes daily performance like a human analyst.
*   **📈 Multi-Factor Strategy (`core/factors.py`)**:
    *   Modular factor generation engine supporting technical indicators (RSI, MACD, Bollinger Bands, etc.).
    *   Dynamic signal strength calculation.
*   **🛡️ Smart Risk Management (`adaptive_stops.py`)**:
    *   **Volatility-Based Stops**: Automatically adjusts stop-loss levels based on market ATR.
    *   **Trailing Stops**: Locks in profits as the trend moves in your favor.
*   **🤖 Automated Execution**:
    *   Full-loop automation: Signal -> Order -> Position Management -> Exit.
*   **🔔 Real-Time Alerts**:
    *   Telegram integration (`notifier.py`) for instant trade notifications.

### 🛠️ Configuration & Usage

**1. Installation**
```bash
git clone https://github.com/Chinghu-web/Crypto-Quant-Core.git
cd Crypto-Quant-Core
pip install -r requirements.txt
```

**2. Configuration (`config.yaml`)**
The `config.yaml` file is included in the repository. Open it directly to set up your strategy parameters:

*   **API Keys**: Enter your OKX / DeepSeek / Claude API keys.
*   **Trading Settings**:
    *   `investment_amount`: The USDT amount allocated for trading.
    *   `leverage`: Leverage ratio (e.g., 10x, 20x).
    *   `take_profit` / `stop_loss`: Set your TP/SL ratios.
    *   `max_open_positions`: Limit the number of concurrent trades.

**3. Run**
```bash
python main.py
```

### 📢 Status & Roadmap
*   **Current Status**: The strategy has performed well in internal testing (`not bad` results).
*   **Roadmap**: I am continuously optimizing the algorithm and the AI prompt engineering.
*   **Follow Me**: Star ⭐ this repo to stay updated! I will keep refining this model to explore how AI can better assist us in crypto trading.

---

<a name="-中文说明"></a>
## 🇨🇳 中文说明

**Crypto-Quant-Core** 是一个基于 Python 开发的现代化加密货币量化交易系统（针对 **OKX** 交易所优化）。

本系统的核心亮点在于**双 AI 引擎驱动 (Claude & DeepSeek)**。它不再是死板的代码逻辑，而是结合了传统量化因子与大语言模型（LLM）的推理能力，让 AI 真正辅助我们进行更聪明的交易。

> ⚠️ **免责声明**
> 本项目为个人量化系统的**代码存档**，仅供学习参考。
> *   **实盘表现**：内部测试结果表现良好，但市场充满不确定性。
> *   **风险提示**：请勿直接将未测试的代码用于实盘资金交易，风险自负。

### ✨ 核心亮点

*   **🧠 双 AI 引擎复盘 (`claude_reviewer.py`)**：
    *   **Claude + DeepSeek 强强联手**：同时接入 DeepSeek（擅长推理与代码）和 Claude（擅长分析与语义），多角度分析市场。
    *   **智能分析**：自动读取交易日志，像人类分析师一样对交易行为进行点评、总结盈亏原因。
*   **📈 多因子策略引擎 (`core/factors.py`)**：
    *   模块化的因子计算层，支持多种技术指标（RSI, MACD, 布林带等）组合。
    *   动态计算信号权重。
*   **🛡️ 自适应风控系统 (`adaptive_stops.py`)**：
    *   **波动率止损**：根据市场 ATR 自动调整止损线。
    *   **移动止损 (Trailing Stop)**：随着盈利增加自动上移止损位，锁定利润。
*   **🤖 全自动交易闭环**：
    *   涵盖从 信号生成 -> 自动下单 -> 仓位管理 -> 止盈止损 的全流程。
*   **🔔 实时监控通知**：
    *   集成 Telegram 机器人，实时推送开平仓信息。

### 🛠️ 配置与使用

**1. 下载与安装**
```bash
git clone https://github.com/Chinghu-web/Crypto-Quant-Core.git
cd Crypto-Quant-Core
pip install -r requirements.txt
```

**2. 核心配置 (`config.yaml`)**
项目中已包含 `config.yaml` 文件，请直接打开并修改以下关键参数：

*   **API 设置**：填入 OKX、DeepSeek、Claude 的 API Key。
*   **交易参数 (Strategy Params)**：
    *   `investment_amount` (开仓金额)：单次或总投入的资金量。
    *   `leverage` (杠杆倍数)：设置合约杠杆（如 5x, 10x）。
    *   `take_profit` (止盈率) / `stop_loss` (止损率)：风险控制的核心参数。
    *   `position_limit` (最大持仓)：限制同时开仓的数量。

**3. 启动系统**
```bash
python main.py
```

### 📢 项目现状与展望
*   **测试反馈**：目前的交易模型在我的内部测试中**结果还可以**，收益曲线相对稳定。
*   **持续迭代**：我正在不断完善策略逻辑和 AI 的 Prompt（提示词），致力于让 AI 更精准地识别市场机会。
*   **关注我**：如果你也对 **AI + Crypto Trading** 感兴趣，请 **Star ⭐** 本项目！我会持续更新调整，让 AI 更好地帮助我们交易虚拟货币。

### 📂 项目结构
```text
Crypto-Quant-Core/
├── core/                   # 核心逻辑 (因子、止损、AI复盘)
├── tools/                  # 工具箱
├── main.py                 # 启动入口
├── config.yaml             # 配置文件 (在此处修改金额、杠杆等)
└── requirements.txt        # 依赖列表
```

## 🤝 License
MIT License
```
