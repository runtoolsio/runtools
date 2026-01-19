# Runtools

A lightweight toolkit for running, managing, and monitoring jobs.

## Why Runtools?

**No framework lock-in.** Unlike heavyweight job frameworks, Runtools doesn't dictate how you write your code. Your business logic stays clean and decoupled - no base classes to inherit, no decorators required, no framework-specific patterns to follow.

**Run anything.** Any script, any command, any executable:
```bash
run job ./my_script.py
run job "python etl.py --date=2024-01-01"
run job ./backup.sh
```

**Built for simplicity.** Perfect for batch jobs, cron tasks, and scripts that don't need the complexity of distributed job frameworks. If you're running scheduled tasks and want visibility into what's happening without adopting a complex infrastructure, Runtools is for you.

**Monitor and manage.** Track job status, view logs, check history - all from the command line:
```bash
taro ps           # View running jobs
taro history      # Browse execution history
taro tail JOB_ID  # Stream job output
taro stop JOB_ID  # Stop a running job
```

## Installation

```bash
pip install runtoolsio
```

This installs all runtools components:
- **[runtools-runcore](https://github.com/runtoolsio/runcore)** - Core library for managing and monitoring jobs, defines runspec API
- **[runtools-runjob](https://github.com/runtoolsio/runjob)** - Job execution engine
- **[runtools-runcli](https://github.com/runtoolsio/runcli)** - CLI for running jobs
- **[runtools-taro](https://github.com/runtoolsio/taro)** - CLI for managing and monitoring jobs

## Individual Packages

You can also install components separately:

```bash
pip install runtools-runcore  # Core library only
pip install runtools-taro     # CLI only
```

## Documentation

Visit [runtools.io](https://runtools.io) for documentation.

## License

MIT
