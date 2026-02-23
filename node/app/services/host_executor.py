"""Host command executor - runs arbitrary commands on host via nsenter

Works from Docker container by using nsenter to execute commands on host.
Requires container to run with: privileged: true, pid: host
"""

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass
from typing import Optional, AsyncGenerator

logger = logging.getLogger(__name__)

# Maximum allowed timeout (10 minutes)
MAX_TIMEOUT = 600
DEFAULT_TIMEOUT = 30

# Extended PATH to include common binary locations (snap, local bins, etc.)
# This ensures commands like speedtest (installed via snap) work from panel
EXTENDED_PATH = "/snap/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"


@dataclass
class ExecuteResult:
    """Result of command execution"""
    success: bool
    exit_code: int
    stdout: str
    stderr: str
    execution_time_ms: int
    error: Optional[str] = None


class HostExecutor:
    """Executes commands on host system via nsenter (for Docker with pid: host)"""
    
    def __init__(self):
        self._use_nsenter = self._check_nsenter_needed()
    
    def _check_nsenter_needed(self) -> bool:
        """Check if we're in a container and need nsenter"""
        if os.path.exists('/.dockerenv'):
            return True
        try:
            with open('/proc/1/cgroup', 'r') as f:
                if 'docker' in f.read():
                    return True
        except Exception:
            pass
        return False
    
    def _prepare_command(self, command: str) -> str:
        """Wrap command with extended PATH to ensure snap/local binaries are accessible"""
        return f'export PATH="{EXTENDED_PATH}:$PATH"; {command}'
    
    async def execute(
        self,
        command: str,
        timeout: int = DEFAULT_TIMEOUT,
        shell: str = "sh"
    ) -> ExecuteResult:
        """
        Execute command on host system.
        
        Args:
            command: Shell command to execute
            timeout: Timeout in seconds (max 600)
            shell: Shell to use (sh or bash)
        
        Returns:
            ExecuteResult with stdout, stderr, exit_code and timing
        """
        # Validate timeout
        timeout = min(max(1, timeout), MAX_TIMEOUT)
        
        # Wrap command with extended PATH
        prepared_command = self._prepare_command(command)
        
        # Build command
        if self._use_nsenter:
            # nsenter flags:
            # -t 1: target PID 1 (init process on host)
            # -m: mount namespace
            # -u: UTS namespace (hostname)
            # -n: network namespace
            # -i: IPC namespace
            # -p: PID namespace
            # -C: cgroup namespace (required for snap apps)
            cmd = [
                "nsenter", "-t", "1", "-m", "-u", "-n", "-i", "-p", "-C",
                "--", shell, "-c", prepared_command
            ]
        else:
            cmd = [shell, "-c", prepared_command]
        
        start_time = time.time()
        
        try:
            logger.info(f"Executing on host: {command[:100]}{'...' if len(command) > 100 else ''}")
            
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            
            try:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(),
                    timeout=timeout
                )
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
                execution_time = int((time.time() - start_time) * 1000)
                logger.warning(f"Command timed out after {timeout}s: {command[:50]}")
                return ExecuteResult(
                    success=False,
                    exit_code=-1,
                    stdout="",
                    stderr="",
                    execution_time_ms=execution_time,
                    error=f"Command timed out after {timeout} seconds"
                )
            
            execution_time = int((time.time() - start_time) * 1000)
            exit_code = process.returncode or 0
            stdout_str = stdout.decode('utf-8', errors='replace').strip()
            stderr_str = stderr.decode('utf-8', errors='replace').strip()
            
            logger.info(
                f"Command completed: exit_code={exit_code}, "
                f"time={execution_time}ms, stdout_len={len(stdout_str)}"
            )
            
            return ExecuteResult(
                success=exit_code == 0,
                exit_code=exit_code,
                stdout=stdout_str,
                stderr=stderr_str,
                execution_time_ms=execution_time
            )
            
        except FileNotFoundError:
            execution_time = int((time.time() - start_time) * 1000)
            error_msg = "nsenter not found" if self._use_nsenter else f"{shell} not found"
            logger.error(f"Command execution failed: {error_msg}")
            return ExecuteResult(
                success=False,
                exit_code=-1,
                stdout="",
                stderr="",
                execution_time_ms=execution_time,
                error=error_msg + " - container must have privileged: true and pid: host"
            )
        except Exception as e:
            execution_time = int((time.time() - start_time) * 1000)
            logger.error(f"Command execution failed: {e}")
            return ExecuteResult(
                success=False,
                exit_code=-1,
                stdout="",
                stderr="",
                execution_time_ms=execution_time,
                error=str(e)
            )
    
    async def execute_stream(
        self,
        command: str,
        timeout: int = DEFAULT_TIMEOUT,
        shell: str = "sh"
    ) -> AsyncGenerator[str, None]:
        """
        Execute command on host system with streaming output (SSE format).
        
        Yields SSE-formatted events:
            event: stdout\ndata: {"line": "..."}\n\n
            event: stderr\ndata: {"line": "..."}\n\n
            event: done\ndata: {"exit_code": 0, "execution_time_ms": 1234}\n\n
            event: error\ndata: {"message": "..."}\n\n
        """
        timeout = min(max(1, timeout), MAX_TIMEOUT)
        
        # Wrap command with extended PATH
        prepared_command = self._prepare_command(command)
        
        if self._use_nsenter:
            cmd = [
                "nsenter", "-t", "1", "-m", "-u", "-n", "-i", "-p", "-C",
                "--", shell, "-c", prepared_command
            ]
        else:
            cmd = [shell, "-c", prepared_command]
        
        start_time = time.time()
        
        def format_sse(event: str, data: dict) -> str:
            return f"event: {event}\ndata: {json.dumps(data)}\n\n"
        
        try:
            logger.info(f"Executing (stream) on host: {command[:100]}{'...' if len(command) > 100 else ''}")
            
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            
            deadline = time.time() + timeout
            stdout_queue: asyncio.Queue = asyncio.Queue()
            stderr_queue: asyncio.Queue = asyncio.Queue()
            
            async def read_stream_to_queue(stream, queue: asyncio.Queue, stream_name: str):
                """Read from stream and put lines into queue"""
                try:
                    while True:
                        line = await stream.readline()
                        if line:
                            decoded = line.decode('utf-8', errors='replace').rstrip('\n\r')
                            await queue.put((stream_name, decoded))
                        else:
                            break
                except Exception as e:
                    logger.debug(f"Stream {stream_name} read ended: {e}")
                finally:
                    await queue.put((stream_name, None))  # Signal end of stream
            
            # Start reader tasks
            stdout_task = asyncio.create_task(read_stream_to_queue(process.stdout, stdout_queue, "stdout"))
            stderr_task = asyncio.create_task(read_stream_to_queue(process.stderr, stderr_queue, "stderr"))
            
            stdout_done = False
            stderr_done = False
            
            while not (stdout_done and stderr_done):
                remaining = deadline - time.time()
                if remaining <= 0:
                    stdout_task.cancel()
                    stderr_task.cancel()
                    process.kill()
                    await process.wait()
                    execution_time = int((time.time() - start_time) * 1000)
                    logger.warning(f"Command timed out after {timeout}s: {command[:50]}")
                    yield format_sse("error", {"message": f"Command timed out after {timeout} seconds"})
                    yield format_sse("done", {"exit_code": -1, "execution_time_ms": execution_time, "success": False})
                    return
                
                # Process stdout queue
                if not stdout_done:
                    try:
                        stream_name, line = await asyncio.wait_for(stdout_queue.get(), timeout=0.05)
                        if line is None:
                            stdout_done = True
                        else:
                            yield format_sse("stdout", {"line": line})
                    except asyncio.TimeoutError:
                        pass
                
                # Process stderr queue
                if not stderr_done:
                    try:
                        stream_name, line = await asyncio.wait_for(stderr_queue.get(), timeout=0.05)
                        if line is None:
                            stderr_done = True
                        else:
                            yield format_sse("stderr", {"line": line})
                    except asyncio.TimeoutError:
                        pass
            
            # Wait for process to finish
            await process.wait()
            await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
            
            execution_time = int((time.time() - start_time) * 1000)
            exit_code = process.returncode or 0
            
            logger.info(f"Command (stream) completed: exit_code={exit_code}, time={execution_time}ms")
            
            yield format_sse("done", {
                "exit_code": exit_code,
                "execution_time_ms": execution_time,
                "success": exit_code == 0
            })
            
        except FileNotFoundError:
            execution_time = int((time.time() - start_time) * 1000)
            error_msg = "nsenter not found" if self._use_nsenter else f"{shell} not found"
            logger.error(f"Command execution failed: {error_msg}")
            yield format_sse("error", {"message": error_msg + " - container must have privileged: true and pid: host"})
            yield format_sse("done", {"exit_code": -1, "execution_time_ms": execution_time, "success": False})
        except Exception as e:
            execution_time = int((time.time() - start_time) * 1000)
            logger.error(f"Command execution failed: {e}")
            yield format_sse("error", {"message": str(e)})
            yield format_sse("done", {"exit_code": -1, "execution_time_ms": execution_time, "success": False})
    
    def execute_sync(
        self,
        command: str,
        timeout: int = DEFAULT_TIMEOUT,
        shell: str = "sh"
    ) -> ExecuteResult:
        """
        Synchronous version of execute for use in non-async contexts.
        """
        import subprocess
        
        timeout = min(max(1, timeout), MAX_TIMEOUT)
        
        # Wrap command with extended PATH
        prepared_command = self._prepare_command(command)
        
        if self._use_nsenter:
            cmd = [
                "nsenter", "-t", "1", "-m", "-u", "-n", "-i", "-p", "-C",
                "--", shell, "-c", prepared_command
            ]
        else:
            cmd = [shell, "-c", prepared_command]
        
        start_time = time.time()
        
        try:
            logger.info(f"Executing on host (sync): {command[:100]}{'...' if len(command) > 100 else ''}")
            
            result = subprocess.run(
                cmd,
                capture_output=True,
                timeout=timeout
            )
            
            execution_time = int((time.time() - start_time) * 1000)
            
            return ExecuteResult(
                success=result.returncode == 0,
                exit_code=result.returncode,
                stdout=result.stdout.decode('utf-8', errors='replace').strip(),
                stderr=result.stderr.decode('utf-8', errors='replace').strip(),
                execution_time_ms=execution_time
            )
            
        except subprocess.TimeoutExpired:
            execution_time = int((time.time() - start_time) * 1000)
            return ExecuteResult(
                success=False,
                exit_code=-1,
                stdout="",
                stderr="",
                execution_time_ms=execution_time,
                error=f"Command timed out after {timeout} seconds"
            )
        except Exception as e:
            execution_time = int((time.time() - start_time) * 1000)
            return ExecuteResult(
                success=False,
                exit_code=-1,
                stdout="",
                stderr="",
                execution_time_ms=execution_time,
                error=str(e)
            )


# Singleton instance
_executor: Optional[HostExecutor] = None


def get_host_executor() -> HostExecutor:
    """Get or create HostExecutor instance"""
    global _executor
    if _executor is None:
        _executor = HostExecutor()
    return _executor
