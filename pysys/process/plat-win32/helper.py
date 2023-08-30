#!/usr/bin/env python
# PySys System Test Framework, Copyright (C) 2006-2022 M.B. Grieve

# This library is free software; you can redistribute it and/or
# modify it under the terms of the GNU Lesser General Public
# License as published by the Free Software Foundation; either
# version 2.1 of the License, or (at your option) any later version.

# This library is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# Lesser General Public License for more details.

# You should have received a copy of the GNU Lesser General Public
# License along with this library; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA

"""Contains the OS-specific process wrapper subclass. 

:meta private: No reason to publically document this. 
"""

import string, os.path, time, logging, sys, threading, platform

import win32api, win32pdh, win32security, win32process, win32file, win32pipe, win32con, pywintypes, win32job, win32event

import queue as Queue

from pysys import process_lock
from pysys.constants import *
from pysys.exceptions import *
from pysys.process import Process

# check for new lines on end of a string
EXPR = re.compile(".*\n$")
log = logging.getLogger('pysys.process')

try:
	IS_PRE_WINDOWS_8 = int(platform.version().split('.')[0]) < 8
except Exception: # pragma: no cover
	IS_PRE_WINDOWS_8 = False
	
class ProcessImpl(Process):
	"""Windows Process wrapper/implementation for process execution and management. 
	
	The process wrapper provides the ability to start and stop an external process, setting 
	the process environment, working directory and state i.e. a foreground process in which case 
	a call to the L{start} method will not return until the process has exited, or a background 
	process in which case the process is started in a separate thread allowing concurrent execution 
	within the testcase. Processes started in the foreground can have a timeout associated with them, such
	that should the timeout be exceeded, the process will be terminated and control	passed back to the 
	caller of the method. The wrapper additionally allows control over logging of the process stdout 
	and stderr to file, and writing to the process stdin.
	
	Usage of the class is to first create an instance, setting all runtime parameters of the process 
	as data attributes to the class instance via the constructor. The process can then be started 
	and stopped via the L{start} and L{stop} methods of the class, as well as interrogated for 
	its executing status via the L{running} method, and waited for its completion via the L{wait}
	method. During process execution the C{self.pid} and C{seld.exitStatus} data attributes are set 
	within the class instance, and these values can be accessed directly via it's object reference.  

	:ivar pid: The process id for a running or complete process (as set by the OS)
	:type pid: integer
	:ivar exitStatus: The process exit status for a completed process	
	:type exitStatus: integer
	
	"""

	def __init__(self, command, arguments, environs, workingDir, state, timeout, stdout=None, stderr=None, displayName=None, **kwargs):
		"""Create an instance of the process wrapper.
		
		:param command:  The full path to the command to execute
		:param arguments:  A list of arguments to the command
		:param environs:  A dictionary of environment variables (key, value) for the process context execution
		:param workingDir:  The working directory for the process
		:param state:  The state of the process (L{pysys.constants.FOREGROUND} or L{pysys.constants.BACKGROUND}
		:param timeout:  The timeout in seconds to be applied to the process
		:param stdout:  The full path to the filename to write the stdout of the process
		:param stderr:  The full path to the filename to write the sdterr of the process
		:param displayName: Display name for this process

		"""
		
		Process.__init__(self, command, arguments, environs, workingDir, 
			state, timeout, stdout, stderr, displayName, **kwargs)

		self.disableKillingChildProcesses = self.info.get('__pysys.disableKillingChildProcesses', False) # currently undocumented, just an emergency escape hatch for now

		assert self.environs, 'Cannot start a process with no environment variables set; use createEnvirons to make a minimal set of env vars'

		# private instance variables
		self.__hProcess = None
		self.__hThread = None
		self.__tid = None
		
		self.__lock = threading.Lock() # to protect access to the fields that get updated

		self.stdout = u'nul' if (not self.stdout) else self.stdout.replace('/',os.sep)
		self.stderr = u'nul' if (not self.stderr) else self.stderr.replace('/',os.sep)
		

		# these different field names are just retained for compatibility in case anyone is using them
		self.fStdout = self.stdout
		self.fStderr = self.stderr


	def _writeStdin(self, data):
		with self.__lock:
			if not self.__stdin: return
			if data is None:
				win32file.CloseHandle(self.__stdin)
			else:
				win32file.WriteFile(self.__stdin, data, None)

	def __quoteCommand(self, input):
		"""Private method to quote a command (argv[0]) "correctly" for Windows.

		The returned value can be used as the start of a command line, with subsequent
		arguments quoted using __quoteArgument() and added to the command line with
		intervening spaces.
		
		The quoted command will be handled correctly by the standard Windows command
		line parsers (CommandLineToArgvW and parse_cmdline), unless the input includes
		double quotes, control characters, or whitespace other than spaces.  The
		behaviour of the standard parsers is different in those cases, and given that
		there is no way of knowing which parser the command will actually use, no
		attempt is made to deal with these differences and the results are undefined.
		"""
		return '\"%s\"' % input if ' ' in input else input

	def __quoteArgument(self, input):
		"""Private method to quote and escape a command line argument correctly for Windows.

		The returned value can be added to the end of a command line, with an intervening
		space, and will be treated as a separate argument by the standard Windows command
		line parsers (CommandLineToArgvW and parse_cmdline).  Double quotes, whitespace
		and backslashes in the argument will be preserved for the parser to see them.
		
		Windows' quoting and escaping rules are somewhat complex and the implementation
		of this method was derived from a few different sources:
		https://docs.microsoft.com/en-us/previous-versions/17w5ykft(v=vs.85)
		http://www.windowsinspired.com/the-correct-way-to-quote-command-line-arguments/
		http://www.windowsinspired.com/understanding-the-command-line-string-and-arguments-received-by-a-windows-program/
		http://www.windowsinspired.com/how-a-windows-programs-splits-its-command-line-into-individual-arguments/
		https://daviddeley.com/autohotkey/parameters/parameters.htm
		This method tries to avoid any areas of different behaviour between the two
		standard parsers, in particular the undocumented rules around handling of
		consecutive unescaped double quote characters.
		"""
		whitespace = None
		# Short-circuit some easy and common cases:
		# - No quotes, no whitespace (just return the input unchanged)
		# - No quotes, no trailing backslash, whitespace (wrap in double quotes)
		# Everything else falls through to the more complex algorithm
		if '\"' not in input:
			empty = (len(input) == 0)
			whitespace = (empty or ' ' in input or '\t' in input)
			if not whitespace: return input
			if not empty and input[-1] != '\\': return '\"%s\"' % input
		# Make sure we look for whitespace exactly once
		if whitespace is None: whitespace = (' ' in input or '\t' in input)

		output = []
		backslash = 0
		for ch in input:
			# Count backslashes until we hit a non-backslash
			if ch == '\\':
				backslash += 1
			elif ch == '\"':
				# Add any pending backslashes (escaped)
				# Then add the escaped double quote
				output.extend([2 * backslash * '\\', '\\\"'])
				backslash = 0
			else:
				# Add any pending backslashes (unescaped)
				# Then add the next character
				output.extend([backslash * '\\', ch])
				backslash = 0
		if whitespace:
			# Add any pending backslashes (escaped)
			# Wrap the whole argument in double quotes
			output.extend([2 * backslash * '\\', '\"'])
			output.insert(0, '\"')
		else:
			# Add any pending backslashes (unescaped)
			output.append(backslash * '\\')
		return ''.join(output)

	def __buildCommandLine(self, command, args):
		""" Private method to build a Windows command line from a command plus argument list.
		
		Returns both the quoted command (argv[0]) and the fully quoted and escaped command
		line (including the command), because both are used elsewhere in this class.
		"""
		new_command = self.__quoteCommand(command)
		command_line = [new_command]
		for arg in args: command_line.append(self.__quoteArgument(arg))
		return new_command, ' '.join(command_line)

	def startBackgroundProcess(self):
		"""Method to start a process running in the background.
		
		"""	
		with process_lock:
			# security attributes for pipes
			sAttrs = win32security.SECURITY_ATTRIBUTES()
			sAttrs.bInheritHandle = 1
	
			# create pipes for the process to write to
			hStdin_r, hStdin = win32pipe.CreatePipe(sAttrs, 0)
			hStdout = win32file.CreateFile(self.stdout, win32file.GENERIC_WRITE | win32file.GENERIC_READ,
			   win32file.FILE_SHARE_DELETE | win32file.FILE_SHARE_READ | win32file.FILE_SHARE_WRITE,
			   sAttrs, win32file.CREATE_ALWAYS, win32file.FILE_ATTRIBUTE_NORMAL, None)
			hStderr = win32file.CreateFile(self.stderr, win32file.GENERIC_WRITE | win32file.GENERIC_READ,
			   win32file.FILE_SHARE_DELETE | win32file.FILE_SHARE_READ | win32file.FILE_SHARE_WRITE,
			   sAttrs, win32file.CREATE_ALWAYS, win32file.FILE_ATTRIBUTE_NORMAL, None)
			  
			try:

				# set the info structure for the new process.
				StartupInfo = win32process.STARTUPINFO()
				StartupInfo.hStdInput  = hStdin_r
				StartupInfo.hStdOutput = hStdout
				StartupInfo.hStdError  = hStderr
				StartupInfo.dwFlags = win32process.STARTF_USESTDHANDLES

				# Create new handles for the thread ends of the pipes. The duplicated handles will
				# have their inheritence properties set to false so that any children inheriting these
				# handles will not have non-closeable handles to the pipes
				pid = win32api.GetCurrentProcess()
				tmp = win32api.DuplicateHandle(pid, hStdin, pid, 0, 0, win32con.DUPLICATE_SAME_ACCESS)
				win32file.CloseHandle(hStdin)
				hStdin = tmp

				# start the process, and close down the copies of the process handles
				# we have open after the process creation (no longer needed here)
				new_command, command_line = self.__buildCommandLine(self.command, self.arguments)
				
				# Windows CreateProcess maximum lpCommandLine length is 32,768
				# http://msdn.microsoft.com/en-us/library/ms682425%28VS.85%29.aspx
				if len(command_line)>=32768: # pragma: no cover
					raise ValueError("Command line length exceeded 32768 characters: %s..."%command_line[:1000])

				dwCreationFlags = 0
				if IS_PRE_WINDOWS_8: # pragma: no cover
					# In case PySys is itself running in a job, might need to explicitly breakaway from it so we can give 
					# it its own, but only for old pre-windows 8/2012, which support nested jobs
					dwCreationFlags  = dwCreationFlags | win32process.CREATE_BREAKAWAY_FROM_JOB
				
				if self.command.lower().endswith(('.bat', '.cmd')):
					# If we don't start suspended there's a slight race condition but due to some issues with 
					# initially-suspended processes hanging (seen many years ago), to be safe, only bother to close the 
					# race condition for shell scripts (which is the main use case for this anyway)
					dwCreationFlags = dwCreationFlags | win32con.CREATE_SUSPENDED

				self.__job = self._createParentJob()

				try:
					self.__hProcess, self.__hThread, self.pid, self.__tid = win32process.CreateProcess( None, command_line, None, None, 1, 
						dwCreationFlags, self.environs, os.path.normpath(self.workingDir), StartupInfo)
				except pywintypes.error as e:
					raise ProcessError("Error creating process %s: %s" % (new_command, e))

				try:
					if not self.disableKillingChildProcesses:
						win32job.AssignProcessToJobObject(self.__job, self.__hProcess)
					else: 
						self.__job = None # pragma: no cover
				except Exception as e: # pragma: no cover
					# Shouldn't fail unless process already terminated (which can happen since 
					# if we didn't use SUSPENDED there's an inherent race here)
					if win32process.GetExitCodeProcess(self.__hProcess)==win32con.STILL_ACTIVE:
						log.warning('Failed to associate process %s with new job: %s (this may prevent automatic cleanup of child processes)' %(self, e))
					
					# force use of TerminateProcess not TerminateJobObject if this failed
					self.__job = None
				
				if (dwCreationFlags & win32con.CREATE_SUSPENDED) != 0:
					win32process.ResumeThread(self.__hThread)
			finally:
				win32file.CloseHandle(hStdin_r)
				win32file.CloseHandle(hStdout)
				win32file.CloseHandle(hStderr)

			# set the handle to the stdin of the process 
			self.__stdin = hStdin

	def _createParentJob(self):
		# Create a new job that this process will be assigned to.
		job_name = '' # must be anonymous otherwise we'd get conflicts
		security_attrs = win32security.SECURITY_ATTRIBUTES()
		security_attrs.bInheritHandle = 1
		job = win32job.CreateJobObject(security_attrs, job_name)
		extended_limits = win32job.QueryInformationJobObject(job, win32job.JobObjectExtendedLimitInformation)
		extended_limits['BasicLimitInformation']['LimitFlags'] = win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
		
		win32job.SetInformationJobObject(job, win32job.JobObjectExtendedLimitInformation, extended_limits)
		return job


	def setExitStatus(self):
		"""Tests whether the process has terminated yet, and updates and returns the exit status if it has. 
		"""
		with self.__lock:
			if self.exitStatus is not None: return self.exitStatus
			exitStatus = win32process.GetExitCodeProcess(self.__hProcess)
			if exitStatus != win32con.STILL_ACTIVE:
				try:
					if self.__hProcess: win32file.CloseHandle(self.__hProcess)
					if self.__hThread: win32file.CloseHandle(self.__hThread)
					if self.__stdin: win32file.CloseHandle(self.__stdin)
				except Exception as e: # pragma: no cover
					# these failed sometimes with 'handle is invalid', probably due to interference of stdin writer thread
					log.warning('Could not close process and thread handles for process %s: %s', self.pid, e) 
				self.__stdin = self.__hThread = self.__hProcess = None
				self._outQueue = None
				self.exitStatus = exitStatus
			
			return self.exitStatus


	def stop(self, timeout=TIMEOUTS['WaitForProcessStop'], hard=False): 
		"""Stop a process running. On Windows this is always a hard termination. 
	
		"""
		try:
			with self.__lock:
				if self.exitStatus is not None: return
				
				try:
					if self.__job:
						win32job.TerminateJobObject(self.__job, 0)
					else:
						win32process.TerminateProcess(self.__hProcess, 0) # pragma: no cover

				except Exception as e: # pragma: no cover
					# ignore errors unless the process is still running
					if win32process.GetExitCodeProcess(self.hProcess)==win32con.STILL_ACTIVE:
						log.warning('Failed to terminate job object for process %s: %s'%(self, e))

						# try this approach instead
						win32process.TerminateProcess(self.__hProcess, 0)

			self.wait(timeout=timeout)
		except Exception as ex: # pragma: no cover
			raise ProcessError("Error stopping process: %s"%ex)


	def _pollWaitUnlessProcessTerminated(self):
		# While waiting for process to terminate, Windows gives us a way to block for completion without polling, so we 
		# can use a larger timeout to avoid wasting time in the Python GIL (but not so large as to stop us from checking for abort

		__hProcess = self.__hProcess # read it atomically

		if __hProcess:
			owner = self.owner
			waitobjects = [__hProcess]
			if owner and (owner.isCleanupInProgress is False) and owner.isInterruptTerminationInProgressEvent: 
				waitobjects.append(owner.isInterruptTerminationInProgressEvent)

			# In theory we could increase this timeout to further reduce contention on the Python GIL but 
			# not doing so yet since in single-threaded mode the interrupt signal is not delivered while the 
			# main thread is busy in the WaitForMultipleObjects call so need to keep this slow to allow responsive Ctrl+C for now
			pollTimeoutMillis = 1000
			if win32event.WaitForMultipleObjects(waitobjects, False, pollTimeoutMillis) != win32event.WAIT_FAILED:
				if owner and owner.isInterruptTerminationInProgress is True and owner.isCleanupInProgress is False: raise KeyboardInterrupt()
				return
		
		self._pollWait() # fallback to a sleep to avoid spinning if an unexpected return code is returned

ProcessWrapper = ProcessImpl # old name for compatibility
