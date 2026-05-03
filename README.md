### Trading

#### claude
python main.py trading -s TSLA --start 2026-01-02 --end 2026-04-30  -m anthropic:claude-sonnet-4-6 <br>
python main.py trading -s MSFT --start 2026-01-02 --end 2026-04-30  -m anthropic:claude-sonnet-4-6 <br>
python main.py trading -s NVDA --start 2026-01-02 --end 2026-04-30  -m anthropic:claude-sonnet-4-6 <br>
python main.py trading -s AAPL --start 2026-01-02 --end 2026-04-30  -m anthropic:claude-sonnet-4-6 <br>

#### openai
python main.py trading -s TSLA --start 2026-01-02 --end 2026-04-30 -m openai:gpt-5.4<br>
python main.py trading -s MSFT --start 2026-01-02 --end 2026-04-30 -m openai:gpt-5.4<br>
python main.py trading -s NVDA --start 2026-01-02 --end 2026-04-30 -m openai:gpt-5.4<br>
python main.py trading -s AAPL --start 2026-01-02 --end 2026-04-30 -m openai:gpt-5.4<br>

#### qwen
python main.py trading -s TSLA --start 2026-01-02 --end 2026-04-30 -m openrouter:qwen/qwen3.5-397b-a17b<br>
python main.py trading -s MSFT --start 2026-01-02 --end 2026-04-30 -m openrouter:qwen/qwen3.5-397b-a17b<br>
python main.py trading -s NVDA --start 2026-01-02 --end 2026-04-30 -m openrouter:qwen/qwen3.5-397b-a17b<br>
python main.py trading -s AAPL --start 2026-01-02 --end 2026-04-30 -m openrouter:qwen/qwen3.5-397b-a17b<br>

python main.py trading -s TSLA --start 2026-01-02 --end 2026-04-30 -m openrouter:qwen/qwen3.5-27b<br>
python main.py trading -s MSFT --start 2026-01-02 --end 2026-04-30 -m openrouter:qwen/qwen3.5-27b<br>
python main.py trading -s NVDA --start 2026-01-02 --end 2026-04-30 -m openrouter:qwen/qwen3.5-27b<br>
python main.py trading -s AAPL --start 2026-01-02 --end 2026-04-30 -m openrouter:qwen/qwen3.5-27b<br>

python main.py trading -s TSLA --start 2026-01-02 --end 2026-04-30 -m openrouter:qwen/qwen3.5-9b<br>
python main.py trading -s MSFT --start 2026-01-02 --end 2026-04-30 -m openrouter:qwen/qwen3.5-9b<br>
python main.py trading -s NVDA --start 2026-01-02 --end 2026-04-30 -m openrouter:qwen/qwen3.5-9b<br>
python main.py trading -s AAPL --start 2026-01-02 --end 2026-04-30 -m openrouter:qwen/qwen3.5-9b<br>


### Hedging
python main.py hedging --start 2026-01-02 --end 2026-04-30 -m anthropic:claude-sonnet-4-6 <br>
python main.py hedging --start 2026-01-02 --end 2026-04-30 -m openai:gpt-5.4 <br>
python main.py hedging --start 2026-01-02 --end 2026-04-30 -m openrouter:qwen/qwen3.5-397b-a17b<br>
python main.py hedging --start 2026-01-02 --end 2026-04-30 -m openrouter:qwen/qwen3.5-27b<br>
python main.py hedging --start 2026-01-02 --end 2026-04-30 -m openrouter:qwen/qwen3.5-9b<br>

### Audting

### Report Generation
python main.py report-generation -s AAPL --start 2026-01-02 --end 2026-04-30 -m anthropic:claude-sonnet-4-6