# agent_guard

One stdlib Python file that stops an unattended agent from burning money.

## Problem

Autonomous agents can run uncontrolled loops, consume unlimited resources, and generate massive costs without proper safeguards. This is a critical issue in real-world deployments.

## Solution

`agent_guard.py` is a simple, dependency-free Python module that provides a kill-switch for autonomous agents. It monitors resource usage and can terminate the agent when predefined thresholds are exceeded.

## Usage

```python
from agent_guard import AgentGuard

# Initialize with default thresholds
guard = AgentGuard()

# Start monitoring
guard.start()

# Your autonomous agent code here
# ...

# Stop monitoring when done
guard.stop()
```

## Custom Thresholds

```python
from agent_guard import AgentGuard

# Custom thresholds
guard = AgentGuard(
    max_runtime_seconds=3600,  # 1 hour max runtime
    max_memory_mb=500,         # 500MB max memory
    max_api_calls=1000,        # 1000 API calls max
    max_cost_usd=10.0          # $10 max cost
)

guard.start()
# ... agent code ...
guard.stop()
```

## How It Works

- **Runtime Monitoring**: Tracks execution time and terminates if exceeded
- **Memory Monitoring**: Checks memory usage and terminates if exceeded
- **API Call Tracking**: Counts API calls and terminates if exceeded
- **Cost Tracking**: Monitors API costs and terminates if exceeded

## License

MIT License - see LICENSE.txt

---

> **Note**: This is part of the [LLM Cost Tracker](https://yknhue.gumroad.com/) ecosystem of tools for managing AI agent costs and preventing runaway loops.