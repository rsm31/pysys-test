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



"""
Writers that process the contents of test output directories by archiving or collecting output files.

"""

__all__ = [
	"TestOutputArchiveWriter", 
	"CollectTestOutputWriter",
	"PythonCoverageWriter",]

import time, stat, logging, sys, io
import zipfile
import tarfile
import locale
import shutil
import shlex
import hashlib

from pysys.constants import *
from pysys.writer.api import *
from pysys.utils.logutils import ColorLogFormatter, stripANSIEscapeCodes, stdoutPrint
from pysys.utils.fileutils import mkdir, deletedir, toLongPathSafe, fromLongPathSafe, pathexists
from pysys.utils.pycompat import openfile
from pysys.exceptions import UserError
from pysys.utils.safeeval import safeEval

log = logging.getLogger('pysys.writer')

class TestOutputArchiveWriter(BaseRecordResultsWriter):
	"""Writer that creates zip of tar.gz/xz archives of each failed test's output directory, 
	producing artifacts that could be uploaded to a CI system or file share to allow the failures to be analysed. 
	
	This writer is enabled when running with ``--record``. If using this writer in conjunction with a CI writer that 
	publishes the generated archives, be sure to include this writer first in the list of writers in your project 
	configuration. 

	Note that the zip file format typically generates much larger files than tar.xz (and tar.gz) so use the latter format 
	where possible. 

	Publishes artifacts with category name "TestOutputArchive" and the directory (unless there are no archives) 
	as "TestOutputArchiveDir" for any enabled `pysys.writer.api.ArtifactPublisher` writers. 

	.. versionadded:: 1.6.0

	The following properties can be set in the project configuration for this writer:		
	"""

	destDir = '__pysys_output_archives.${outDirName}/'
	"""
	The directory to write the archives to, as an absolute path, or relative to the testRootDir (or --outdir if specified). 

	This directory will be deleted at the start of the run if it already exists. 
	
	Project ``${...}`` properties can be used in the path. 
	"""
	
	format = "zip"
	"""
	The archive type. Supported types are ``zip``, ``tar.gz`` and ``tar.xz``. The latter are often significantly smaller than zip 
	files due to cross-file compression. 

	.. versionadded:: 2.2
	"""

	maxTotalSizeMB = 1024.0
	"""
	The (approximate) limit on the total size of all archives.
	"""
	
	maxArchiveSizeMB = 200.0
	"""
	The (approximate) limit on the size each individual test ``zip`` file, or of the total uncompressed size of the files if making a ``tar.*`` file. 
	"""
	
	maxArchives = 50
	"""
	The maximum number of archives to create. 
	"""
	
	archiveAtEndOfRun = True # if at end of run can give deterministic order, also reduces I/O while tests are executing
	"""
	By default all archives are created at the end of the run once all tests have finished executing. This avoids 
	I/O contention with execution of tests, and also selection of the tests to generated archives to be done 
	in a deterministic (but pseudo-random) fashion rather than just taking the first N failures. 
	
	Alternatively you can this property to false if you wish to create archives during the test run as each failure 
	occurs. 
	"""


	includeNonFailureOutcomes = 'REQUIRES INSPECTION'
	"""
	In addition to failure outcomes, any outcomes listed here (as comma-separated display names) will be archived. 
	"""

	fileExcludesRegex = u''
	"""
	A regular expression indicating test output paths that will be excluded from archiving, for example large 
	temporary files that are not useful for diagnosing problems. 
	
	For example ``".*/MyTest_001/.*/mybigfile.*[.]tmp"``.
	
	The expression is matched against the path of each output file relative to the test root dir, 
	using forward slashes as the path separator. Multiple paths can be specified using "(path1|path2)" syntax. 
	"""
	
	fileIncludesRegex = u'' # executed against the path relative to the test root dir e.g. (pattern1|pattern2)
	"""
	A regular expression indicating test output paths that will be included in the archive. This can be used to 
	archive just some particular files. Note that for use cases such as collecting graphs and code coverage files 
	generated by a test run, the collect-test-output feature is usually a better fit than using this writer. 
	
	The expression is matched against the path of each output file relative to the test root dir, 
	using forward slashes as the path separator. Multiple paths can be specified using "(path1|path2)" syntax. 
	"""
	
	def setup(self, numTests=0, cycles=1, xargs=None, threads=0, testoutdir=u'', runner=None, **kwargs):
		for k in self.pluginProperties: 
			if not hasattr(type(self), k): raise UserError('Unknown property "%s" for %s'%(k, self))

		self.runner = runner
		if not self.destDir: raise Exception('Cannot set destDir to ""')
		
		# avoid double-expanding (which could mess up ${$} escapes), but if using default value we need to expand it
		if self.destDir == TestOutputArchiveWriter.destDir: self.destDir = runner.project.expandProperties(self.destDir)
		self.destDir = toLongPathSafe(os.path.normpath(os.path.join(runner.output+'/..', self.destDir)))
		if os.path.exists(self.destDir) and all(f.endswith(('.txt', '.zip', '.tar.gz', '.tar.xz')) for f in os.listdir(self.destDir)):
			deletedir(self.destDir) # remove any existing archives (but not if this dir seems to have other stuff in it!)

		self.fileExcludesRegex = re.compile(self.fileExcludesRegex) if self.fileExcludesRegex else None
		self.fileIncludesRegex = re.compile(self.fileIncludesRegex) if self.fileIncludesRegex else None

		self.__totalBytesRemaining = int(float(self.maxTotalSizeMB)*1024*1024)

		if self.archiveAtEndOfRun:
			self.queuedInstructions = []


		self.skippedTests = []
		self.archivesCreated = 0
		
		self.includeNonFailureOutcomes = [str(o) for o in OUTCOMES] if self.includeNonFailureOutcomes=='*' else [o.strip().upper() for o in self.includeNonFailureOutcomes.split(',') if o.strip()]
		for o in self.includeNonFailureOutcomes:
			if not any(o == str(outcome) for outcome in OUTCOMES):
				raise UserError('Unknown outcome display name "%s" in includeNonFailureOutcomes'%o)

	def cleanup(self, **kwargs):
		if self.archiveAtEndOfRun:
			for _, id, outputDir in sorted(self.queuedInstructions): # sort by hash of testId so make order deterministic but also give a varied distribution of ids
				self._archiveTestOutputDir(id, outputDir)
		
		if self.skippedTests:
			# if we hit a limit, at least record the names of the tests we missed
			mkdir(self.destDir)
			with openfile(self.destDir+os.sep+'skipped_artifacts.txt', 'w', encoding='utf-8') as f:
				f.write('\n'.join(os.path.normpath(t) for t in self.skippedTests))
		
		(log.info if self.archivesCreated else log.debug)('%s created %d test output archive artifacts in: %s', 
			self.__class__.__name__, self.archivesCreated, self.destDir)

		if self.archivesCreated:
			self.runner.publishArtifact(self.destDir, 'TestOutputArchiveDir')

	def shouldArchive(self, testObj, **kwargs):
		"""
		Decides whether this test is eligible for archiving of its output. 
		
		The default implementation archives only tests that have a failure outcome, or are listed in 
		``includeNonFailureOutcomes``, but this can be customized if needed by subclasses. 
		
		:param pysys.basetest.BaseTest testObj: The test object under consideration.
		:return bool: True if this test's output can be archived. 
		"""
		return testObj.getOutcome().isFailure() or str(testObj.getOutcome()) in self.includeNonFailureOutcomes


	def processResult(self, testObj, cycle=0, testTime=0, testStart=0, runLogOutput=u'', **kwargs):
		if not self.shouldArchive(testObj): return 
		
		id = ('%s.cycle%03d'%(testObj.descriptor.id, testObj.testCycle)) if testObj.testCycle else testObj.descriptor.id
		
		if self.archiveAtEndOfRun:
			self.queuedInstructions.append([ hashlib.sha1(id.encode('utf-8')).hexdigest(), id, testObj.output]) # need a stable hash (not "hash()") to get a varied but deterministic set of ids
		else:
			self._archiveTestOutputDir(id, testObj.output)
	
	def _newArchive(self, id, **kwargs):
		"""
		Creates and opens a new archive file for the specified id.
		
		:return: (str path, filehandle) The path will include an appropriate extension for this archive type. 
		  The filehandle must have the same API as Python's ZipFile class. 
		"""
		path = self.destDir+os.sep+('%s.%s.%s'%(id, self.runner.project.properties['outDirName'], self.format))
		if self.format == 'zip':
			return path, zipfile.ZipFile(path, 'w', zipfile.ZIP_DEFLATED, allowZip64=True)
		assert self.format.startswith('tar.'), 'Supported formats are: zip, tar.gz, tar.xz not "%s"'%self.format
		return path, tarfile.open(path, 'w:'+self.format.split('.')[1])

	def _archiveTestOutputDir(self, id, outputDir, **kwargs):
		"""
		Creates an archive for the specified test, unless doing so would violate the configured limits 
		(e.g. maxArchives). 
		
		:param str id: The testId (plus a cycle suffix if it's a multi-cycle run). 
		:param str outputDir: The path of the test output dir. 
		"""
		if self.archivesCreated == 0: mkdir(self.destDir)

		if self.archivesCreated == self.maxArchives:
			self.skippedTests.append(outputDir)
			log.debug('Skipping archiving for %s as maxArchives limit is reached', id)
			return
		if self.__totalBytesRemaining < 500:
			self.skippedTests.append(outputDir)
			log.debug('Skipping archiving for %s as maxTotalMB limit is reached', id)
			return
		self.archivesCreated += 1

		try:
			outputDir = toLongPathSafe(outputDir)
			skippedFiles = []
			
			# this is performance-critical so worth caching these
			fileExcludesRegex = self.fileExcludesRegex
			fileIncludesRegex = self.fileIncludesRegex
			isPurgableFile = self.runner.isPurgableFile
			
			bytesRemaining = min(int(self.maxArchiveSizeMB*1024*1024), self.__totalBytesRemaining)
			triedTmpZipFile = False
			
			
			zippath, myzip = self._newArchive(id)
			filesInZip = 0
			with myzip:
				rootlen = len(outputDir) + 1

				for base, dirs, files in os.walk(outputDir):
					# Just the files, don't bother with the directories for now
					files.sort(key=lambda fn: [fn!='run.log', fn] ) # be deterministic, and put run.log first
					dirs.sort()
					
					for f in files:
						fn = os.path.join(base, f)
						if fileExcludesRegex is not None and fileExcludesRegex.search(fn.replace('\\','/')):
							skippedFiles.append(fn)
							continue
						if fileIncludesRegex is not None and not fileIncludesRegex.search(fn.replace('\\','/')):
							skippedFiles.append(fn)
							continue
						
						fileSize = os.path.getsize(fn)
						if fileSize == 0:
							# Since (if not waiting until end) this gets called before testComplete has had a chance to clean things up, skip the 
							# files that it would have deleted. Don't bother listing these in skippedFiles since user 
							# won't be expecting them anyway
							continue
						
						if bytesRemaining < 500:
							skippedFiles.append(fn)
							continue
						
						try:
							if fileSize > bytesRemaining:
								if triedTmpZipFile or self.format!='zip': # to save effort, don't keep trying once we're close - from now on only attempt small files; also not possible if making a tar
									skippedFiles.append(fn)
									continue
								triedTmpZipFile = True
								
								# Only way to know if it'll fit is to try compressing it
								log.debug('File size of %s might push the archive above the limit; creating a temp zip to check', fn)
								tmpname, tmpzip = self._newArchive(id+'.tmp')
								try:
									with tmpzip:
										tmpzip.write(fn, 'tmp')
										compressedSize = tmpzip.getinfo('tmp').compress_size
										if compressedSize > bytesRemaining:
											log.debug('Skipping file as compressed size of %s bytes exceeds remaining limit of %s bytes: %s', 
												compressedSize, bytesRemaining, fn)
											skippedFiles.append(fn)
											continue
								finally:
									os.remove(tmpname)
									
							# Here's where we actually add it to the real archive
							memberName = fn[rootlen:].replace('\\','/')
							if self.format == 'zip':
								myzip.write(fn, memberName)
							else:
								myzip.add(fn, memberName)
						except Exception as ex: # might happen due to file locking or similar
							log.warning('Failed to add output file "%s" to archive: %s', fn, ex)
							skippedFiles.append(fn)
							continue
						filesInZip += 1
						if self.format == 'zip':
							bytesRemaining -= myzip.getinfo(memberName).compress_size
						else:
							bytesRemaining -= myzip.getmember(memberName).size # no way to get compressed size unfortunately
				
				if skippedFiles and fileIncludesRegex is None: # keep the archive clean if there's an explicit include
					skippedFilesStr = os.linesep.join([fromLongPathSafe(f) for f in skippedFiles])
					skippedFilesStr = skippedFilesStr.encode('utf-8')
					if self.format == 'zip':
						myzip.writestr('__pysys_skipped_archive_files.txt', skippedFilesStr)
					else:
						tarinfo = tarfile.TarInfo('__pysys_skipped_archive_files.txt')
						tarinfo.size = len(skippedFilesStr)
						myzip.addfile(tarinfo, fileobj=io.BytesIO(skippedFilesStr))
	
			if filesInZip == 0:
				# don't leave empty zips around
				log.debug('No files added to zip so deleting: %s', zippath)
				self.archivesCreated -= 1
				os.remove(zippath)
				return
	
			self.__totalBytesRemaining -= os.path.getsize(zippath)
			self.runner.publishArtifact(zippath, 'TestOutputArchive')
	
		except Exception:
			self.skippedTests.append(outputDir)
			raise
		
class CollectTestOutputWriter(BaseRecordResultsWriter, TestOutputVisitor):
	"""Writer that collects files matching a specified pattern from the output directory after each test, and puts 
	them in a single directory or archive - for example code coverage files or performance graphs. 
	
	This writer can be used as-is or as a base class for writers that need to collect files during test execution 
	then do something with them during cleanup, for example generate a code coverage report. 
	
	Empty files are ignored. 
	
	This writer is always enabled. 

	.. versionadded:: 1.6.0

	The following properties can be set in the project configuration for this writer:		
	"""

	destDir = ''
	"""
	The directory in which the files will be collected, as an absolute path, or relative to the testRootDir (or --outdir if specified). 

	This directory will be deleted at the start of the run if it already exists. 
	
	Project ``${...}`` properties can be used in the path. 
	"""

	destArchive = ''
	"""
	Optional filename of a .zip archive to generate with the contents of the destDir. 
	
	If a non-absolute path is specified it is evaluated relative to the destDir. 
	
	Project ``${...}`` properties can be used in the path. 
	"""

	includeTestIf = ''
	"""
	A Python lambda that will be evaluated at the end of a test to determine whether output from a given test should be collected. 

	For example code coverage collectors built on this class can include only unit tests or only tests that run in 
	pull requests/CI (to ensure a stable baseline for coverage comparisons). This is useful if you wish to have multiple coverage writers 
	to generate separate coverage reports for all correctness/integration tests versus seeing the coverage achieved in your 
	unit tests (or a small set of smoke tests used in pull requests). 

	Note that this option only disables the collection/aggregation it does not do anything to actually disable the generation of the files, 
	so do not use it for excluding performance/soak/reliability tests from code coverage. For that purpose set the 
	``disableCoverage`` group on the relevant tests (possibly using ``pysysdirconfig.xml`` at the directory level) or the set 
	``self.disableCoverage = True`` on the test object, which will prevent any coverage-related slowdown in the test execution. 

	For example::

		<property name="includeTestIf">lambda testObj: 
			'unitTest' in testObj.descriptor.groups
			or testObj.project.getProperty('isLocalDeveloperTestRun',False)
		</property>
	
	The expression is evaluated using the `pysys.utils.safeeval.safeEval` function. 

	.. versionadded:: 2.2
	"""

	fileIncludesRegex = u'' # executed against the path relative to the test root dir e.g. (pattern1|pattern2)
	"""
	A regular expression indicating the test output paths that will be collected. This can be used to 
	archive just some particular files. This is required. 
	
	The expression is matched against the final characters of each output file's path (with the test root dir stripped 
	off), using forward slashes as the path separator. Multiple paths can be specified using "(path1|path2)" syntax. 
	"""

	fileExcludesRegex = u''
	"""
	A regular expression indicating test output paths that will be excluded from collection. 
	
	For example ``".*/MyTest_001/.*/mybigfile.*[.]tmp"``.
	
	The expression is matched against the path as described for fileIncludesRegex.
	"""

	outputPattern = u'@TESTID@.@FILENAME@.@UNIQUE@.@FILENAME_EXT@' 
	"""
	A string indicating the file (and optionally subdirectory name) to use when writing each collected file to 
	the destDir. 
	
	In addition to any standard ``${...}`` property variables from the project 
	configuration, the output pattern can contain these ``@...@`` 
	substitutions:

		- ``@FILENAME@`` is the original base filename with directory and extension removed, to which you 
		  can add prefixes or suffixes as desired. 

		- ``.@FILENAME_EXT@`` is the filename extension, such that the original filename 
		  is ``@FILENAME@.@FILENAME_EXT@`` (note the dot prefix is mandatory here, and will be replaced with 
		  empty string is there is no extension). 

		- ``@TESTID@`` is replaced by the identifier of the test that generated the 
		  output file (including mode suffix if present), which may be useful for tracking where each one came from. 

		- ``@UNIQUE@`` is replaced by a number that ensures the file does not clash 
		  with any other collected output file from another test. The ``@UNIQUE@`` 
		  substitution variable is mandatory. 
	"""
	
	publishArtifactDirCategory = u'' 
	"""
	If specified, the output directory will be published as an artifact using the specified category name, 
	e.g. ``MyCodeCoverageDir``. 
	"""

	publishArtifactArchiveCategory = u'' 
	"""
	If specified the ``destArchive`` file (if any) will be published as an artifact using the specified category name.
	"""

	def isEnabled(self, record=False, **kwargs): 
		return True

	def setup(self, numTests=0, cycles=1, xargs=None, threads=0, testoutdir=u'', runner=None, **kwargs):
		for k in self.pluginProperties: 
			if not hasattr(type(self), k): raise UserError('Unknown property "%s" for %s'%(k, self))
		
		self.runner = runner
		if not self.destDir: raise Exception('Cannot set destDir to ""')
		if not self.fileIncludesRegex: raise Exception('fileIncludesRegex must be specified for %s'%type(self).__name__)

		self.destDir = os.path.normpath(os.path.join(runner.output+'/..', self.destDir))
		if pathexists(self.destDir+os.sep+'pysysproject.xml'): raise Exception('Cannot set destDir to testRootDir')
		
		# the code below assumes (for long path safe logic) this includes correct slashes (if any)
		self.outputPattern = self.outputPattern.replace('/',os.sep).replace('\\', os.sep)
		
		if self.destArchive: self.destArchive = os.path.join(self.destDir, self.destArchive)
		
		if os.path.exists(self.destDir):
			deletedir(self.destDir) # remove any existing archives (but not if this dir seems to have other stuff in it!)
		
		def prepRegex(exp):
			if not exp: return None
			if not exp.endswith('$'): exp = exp+'$' # by default require regex to match up to the end to avoid common mistakes
			return re.compile(exp)

		self.fileExcludesRegex = prepRegex(self.fileExcludesRegex)
		self.fileIncludesRegex = prepRegex(self.fileIncludesRegex)
		
		self.collectedFileCount = 0

	def visitTestOutputFile(self, testObj, path, **kwargs):
		if self.includeTestIf and self.includeTestIf.strip() and not safeEval('(%s)'%self.includeTestIf)(testObj):
			return False

		# strip off test root dir prefix for the regex comparison
		cmppath = fromLongPathSafe(path)
		if cmppath.startswith(self.runner.project.testRootDir):
			cmppath = cmppath[len(self.runner.project.testRootDir)+1:]
		cmppath = cmppath.replace('\\','/')

		if not self.fileIncludesRegex.search(cmppath): 
			#log.debug('skipping file due to fileIncludesRegex: %s', cmppath)
			return False
		
		fileExcludesRegex = self.fileExcludesRegex
		if fileExcludesRegex is not None and fileExcludesRegex.search(cmppath): 
			#log.debug('skipping file due to fileExcludesRegex: %s', cmppath)
			return False
		self.collectPath(testObj, path, **kwargs)

	def collectPath(self, testObj, path, **kwargs):
		name, ext = os.path.splitext(os.path.basename(path))
		collectdest = toLongPathSafe(os.path.join(self.destDir, (self.outputPattern
			.replace('@TESTID@', str(testObj))
			.replace('@FILENAME@', name)
			.replace('.@FILENAME_EXT@', ext)
			)))
		i = 1
		while pathexists(collectdest.replace('@UNIQUE@', '%d'%(i))):
			i += 1
		collectdest = collectdest.replace('@UNIQUE@', '%d'%(i))
		mkdir(os.path.dirname(collectdest))
		shutil.copyfile(toLongPathSafe(path.replace('/',os.sep)), collectdest)
		self.collectedFileCount += 1
	
	def archiveAndPublish(self):
		"""
		Generate an archive of the destDir (if configured) and publish artifacts (if configured). 
		
		Called by default as part of `cleanup()`.
		"""
		if self.destArchive:
			mkdir(os.path.dirname(toLongPathSafe(self.destArchive)))
			with zipfile.ZipFile(toLongPathSafe(self.destArchive), 'w', zipfile.ZIP_DEFLATED, allowZip64=True) as archive:
				rootlen = len(self.destDir)
				for base, dirs, files in os.walk(self.destDir):
					for f in files:
						if os.path.normpath(os.path.join(base, f))==os.path.normpath(self.destArchive): continue
						fn = os.path.join(base, f)
						
						destname = fn[rootlen:].replace('\\','/').lstrip('/')
						
						try:
							try:
								archive.write(fn, destname)
							except PermissionError: # pragma: no cover - can happen on windows due to file system locking issues
								time.sleep(5.0)
								archive.write(fn, destname)
						except Exception as ex: # pragma: no cover
							# Deal with failures (even after retry) - don't abort the whole archive 
							# (e.g. a locked .err file in coverage output dir doesn't matter)
							log.warning('Could not write file to archive %s: "%s" - %s: %s', os.path.basename(self.destArchive), fn, 
								ex.__class__.__name__, ex)
							archive.writestr(destname+'.pysyserror.txt', '!!! PySys could not write this file to the archive - %s: %s'%(
								ex.__class__.__name__, ex))

		if self.publishArtifactDirCategory:
			self.runner.publishArtifact(self.destDir, self.publishArtifactDirCategory)
		if self.publishArtifactArchiveCategory and self.destArchive:
			self.runner.publishArtifact(self.destArchive, self.publishArtifactArchiveCategory)

	def cleanup(self, **kwargs):
		if not pathexists(self.destDir): 
			log.debug('No matching output files were found for collection directory: %s', os.path.normpath(self.destDir))
			return

		log.info('Collected %s test output files to directory: %s', '{:}'.format(self.collectedFileCount), os.path.normpath(fromLongPathSafe(self.destDir)))
		self.archiveAndPublish()
		

# for compatibility with 1.6.0/1.6.1
from pysys.writer.coverage import PythonCoverageWriter
