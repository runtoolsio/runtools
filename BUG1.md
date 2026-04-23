```
 2026-04-23 03:48:35,470 - event=[v2_first_pass_iteration] lineNumber=[40000] dataLines=[39557]



  ^C^CERROR - Output observer error line=2026-04-23 03:48:35,470 - event=[v2_first_pass_iteration] lineNumber=[40000] dataLines=[39557]
    + Exception Group Traceback (most recent call last):
    |   File "/home/ec2-user/.local/share/pipx/venvs/runtoolsio-runcli/lib64/python3.12/site-packages/runtools/runjob/program.py", line 98, in _process_output
    |     run_ctx.output_sink.new_output(line_stripped, is_err, source=self.id)
    |   File "/home/ec2-user/.local/share/pipx/venvs/runtoolsio-runcli/lib64/python3.12/site-packages/runtools/runjob/output/__init__.py", line 230, in new_output
    |     self._output_notification.observer_proxy.new_output(output_line)
    |   File "/home/ec2-user/.local/share/pipx/venvs/runtoolsio-runcli/lib64/python3.12/site-packages/runtools/runcore/util/observer.py", line 120, in method
    |     raise ExceptionGroup("Observer exception(s) occurred", exceptions)
    | ExceptionGroup: Observer exception(s) occurred (1 sub-exception)
    +-+---------------- 1 ----------------
      | Traceback (most recent call last):
      |   File "/home/ec2-user/.local/share/pipx/venvs/runtoolsio-runcli/lib64/python3.12/site-packages/runtools/runcore/util/observer.py", line 112, in method
      |     getattr(observer, name)(*args, **kwargs)
      |   File "/home/ec2-user/.local/share/pipx/venvs/runtoolsio-runcli/lib64/python3.12/site-packages/runtools/runjob/output/__init__.py", line 372, in new_output
      |     storage.store_line(output_line)
      |   File "/home/ec2-user/.local/share/pipx/venvs/runtoolsio-runcli/lib64/python3.12/site-packages/runtools/runjob/output/file.py", line 44, in store_line
      |     self._write_line(line)
      |   File "/home/ec2-user/.local/share/pipx/venvs/runtoolsio-runcli/lib64/python3.12/site-packages/runtools/runjob/output/file.py", line 55, in _write_line
      |     self._file.write(raw)
      | ValueError: write to closed file
      +------------------------------------
  ERROR - Output observer error line=2026-04-23 03:48:35,320 - event=[missing_category] product=[46S4ALFA3M]
    + Exception Group Traceback (most recent call last):
    |   File "/home/ec2-user/.local/share/pipx/venvs/runtoolsio-runcli/lib64/python3.12/site-packages/runtools/runjob/program.py", line 98, in _process_output
    |     run_ctx.output_sink.new_output(line_stripped, is_err, source=self.id)
    |   File "/home/ec2-user/.local/share/pipx/venvs/runtoolsio-runcli/lib64/python3.12/site-packages/runtools/runjob/output/__init__.py", line 230, in new_output
    |     self._output_notification.observer_proxy.new_output(output_line)
    |   File "/home/ec2-user/.local/share/pipx/venvs/runtoolsio-runcli/lib64/python3.12/site-packages/runtools/runcore/util/observer.py", line 120, in method
    |     raise ExceptionGroup("Observer exception(s) occurred", exceptions)
    | ExceptionGroup: Observer exception(s) occurred (1 sub-exception)
    +-+---------------- 1 ----------------
      | Traceback (most recent call last):
      |   File "/home/ec2-user/.local/share/pipx/venvs/runtoolsio-runcli/lib64/python3.12/site-packages/runtools/runcore/util/observer.py", line 112, in method
      |     getattr(observer, name)(*args, **kwargs)
      |   File "/home/ec2-user/.local/share/pipx/venvs/runtoolsio-runcli/lib64/python3.12/site-packages/runtools/runjob/output/__init__.py", line 372, in new_output
      |     storage.store_line(output_line)
      |   File "/home/ec2-user/.local/share/pipx/venvs/runtoolsio-runcli/lib64/python3.12/site-packages/runtools/runjob/output/file.py", line 44, in store_line
      |     self._write_line(line)
      |   File "/home/ec2-user/.local/share/pipx/venvs/runtoolsio-runcli/lib64/python3.12/site-packages/runtools/runjob/output/file.py", line 55, in _write_line
      |     self._file.write(raw)
      | ValueError: write to closed file
      +------------------------------------
  [ec2-user@ip-172-31-96-203 ~]$ ^C

  Any idea how this happened?
```

Root cause:
- Final run persistence currently happens too early. The root `Stage.ENDED` lifecycle snapshot is persisted before `OutputRouter` closes and finalizes compression, so `output_locations` can still point at `.jsonl` while the final artifact becomes `.jsonl.gz`.
- A naive fix that closes output early is wrong: trailing program output can still arrive after the root phase ends, which causes `ValueError: write to closed file` and/or silently dropped output.

What needs to happen:
- Move final persisted run state to a point after output capture has fully drained and `OutputRouter` has closed.
- Keep a single authoritative final persistence path; avoid ad hoc re-persist-after-close patches if possible.
