#!/usr/bin/env python
# PySys System Test Framework, Copyright (C) 2006-2020 M.B. Grieve

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
The `Project <pysys.xml.project.Project>` class holds the ``pysysproject.xml`` project configuration, including all 
user-defined project properties. 

"""

__all__ = ['Project'] # Project is the only member we expose/document from this module

import os.path, logging, xml.dom.minidom, collections, codecs, time
import platform
import locale

import pysys
import pysys.utils.misc
from pysys.constants import *
from pysys import __version__
from pysys.utils.loader import import_module
from pysys.utils.logutils import ColorLogFormatter, BaseLogFormatter
from pysys.utils.fileutils import mkdir, loadProperties
from pysys.utils.pycompat import openfile, makeReadOnlyDict
from pysys.exceptions import UserError

log = logging.getLogger('pysys.xml.project')

class XMLProjectParser(object):
	"""
	:meta private: Not public API. 
	"""
	def __init__(self, dirname, file, outdir):
		self.dirname = dirname
		self.xmlfile = os.path.join(dirname, file)
		log.debug('Loading project file: %s', self.xmlfile)
		self.environment = 'env'
		
		# project load time is a reasonable proxy for test start time, 
		# and we might want to substitute the date/time into property values
		self.startTimestamp = time.time()
		
		self.properties = {
			'testRootDir':self.dirname,
			
			'outDirName':os.path.basename(outdir),
			
			'startDate':time.strftime('%Y-%m-%d', time.localtime(self.startTimestamp)),
			'startTime':time.strftime('%H.%M.%S', time.localtime(self.startTimestamp)),
			'startTimeSecs':'%0.3f'%self.startTimestamp,

			'hostname':HOSTNAME.lower().split('.')[0],
			'os':platform.system().lower(), # e.g. 'windows', 'linux', 'darwin'; a more modern alternative to OSFAMILY

			# old names
			'root':self.dirname, # old name for testRootDir
			'osfamily':OSFAMILY, 
		}
		
		if not os.path.exists(self.xmlfile):
			raise Exception("Unable to find supplied project file \"%s\"" % self.xmlfile)
		
		try:
			self.doc = xml.dom.minidom.parse(self.xmlfile)
		except Exception:
			raise Exception(sys.exc_info()[1])
		else:
			if self.doc.getElementsByTagName('pysysproject') == []:
				raise Exception("No <pysysproject> element supplied in project file")
			else:
				self.root = self.doc.getElementsByTagName('pysysproject')[0]


	def checkVersions(self):
		requirespython = self.root.getElementsByTagName('requires-python')
		if requirespython and requirespython[0].firstChild: 
			requirespython = requirespython[0].firstChild.nodeValue
			if requirespython:
				if list(sys.version_info) < list(map(int, requirespython.split('.'))):
					raise UserError('This test project requires Python version %s or greater, but this is version %s (from %s)'%(requirespython, '.'.join([str(x) for x in sys.version_info[:3]]), sys.executable))

		requirespysys = self.root.getElementsByTagName('requires-pysys')
		if requirespysys and requirespysys[0].firstChild: 
			requirespysys = requirespysys[0].firstChild.nodeValue
			if requirespysys:
				thisversion = __version__
				if pysys.utils.misc.compareVersions(requirespysys, thisversion) > 0:
					raise UserError('This test project requires PySys version %s or greater, but this is version %s'%(requirespysys, thisversion))


	def unlink(self):
		if self.doc: self.doc.unlink()	


	def getProperties(self):
		propertyNodeList = [element for element in self.root.getElementsByTagName('property') if element.parentNode == self.root]

		for propertyNode in propertyNodeList:

			# use of these options for customizing the property names of env/root/osfamily is no longer encouraged; just kept for compat
			if propertyNode.hasAttribute("environment"):
				self.environment = propertyNode.getAttribute("environment")
			elif propertyNode.hasAttribute("root"): 
				propname = propertyNode.getAttribute("root")
				self.properties[propname] = self.dirname
				log.debug('Setting project property %s="%s"', propname, self.dirname)
			elif propertyNode.hasAttribute("osfamily"): # just for older configs, better to use ${os} now
				propname = propertyNode.getAttribute("osfamily")
				self.properties[propname] = OSFAMILY
				log.debug('Setting project property %s="%s"', propname, OSFAMILY)
					
			elif propertyNode.hasAttribute("file"): 
				file = self.expandProperties(propertyNode.getAttribute("file"), default=propertyNode, name='properties file reading')
				self.getPropertiesFromFile(os.path.normpath(os.path.join(self.dirname, file)) if file else '', 
					pathMustExist=(propertyNode.getAttribute("pathMustExist") or '').lower()=='true')
			
			elif propertyNode.hasAttribute("name"):
				name = propertyNode.getAttribute("name") 
				value = self.expandProperties(propertyNode.getAttribute("value"), default=propertyNode, name=name)
				if name in self.properties:
					raise UserError('Cannot set project property "%s" as it is already set'%name)
				self.properties[name] = value
				log.debug('Setting project property %s="%s"', name, value)

				if (propertyNode.getAttribute("pathMustExist") or '').lower()=='true':
					if not (value and os.path.exists(os.path.join(self.dirname, value))):
						raise UserError('Cannot find path referenced in project property "%s": "%s"'%(
							name, '' if not value else os.path.normpath(os.path.join(self.dirname, value))))
			else:
				raise UserError('Found <property> with no name= or file=')
			
			for att in range(propertyNode.attributes.length):
				attName = propertyNode.attributes.item(att).name
				if attName not in {'root', 'osfamily', 'name', 'value', 'environment', 'default', 'file', 'pathMustExist'}: 
					# not an error, to allow for adding new ones in future pysys versions, but worth warning about
					log.warn('Unknown <property> attribute "%s" in project configuration'%attName)

		return self.properties



	def getPropertiesFromFile(self, file, pathMustExist=False):
		if not os.path.isfile(file):
			if pathMustExist:
				raise UserError('Cannot find properties file referenced in %s: "%s"'%(
					self.xmlfile, file))

			log.debug('Skipping project properties file which not exist: "%s"', file)
			return

		try:
			props = loadProperties(file) # since PySys 1.6.0 this is UTF-8 by default
		except UnicodeDecodeError:
			# fall back to ISO8859-1 if not valid UTF-8 (matching Java 9+ behaviour)
			props = loadProperties(file, encoding='iso8859-1')
		
		for name, value in props.items():
			# when loading properties files it's not so helpful to give errors (and there's nowhere else to put an empty value) so default to empty string
			value = self.expandProperties(value, default='', name=name)	
			
			if name in self.properties and value != self.properties[name]:
				# Whereas we want a hard error for duplicate <property name=".../> entries, for properties files 
				# there's a good case to allow overwriting of properties, but it log it at INFO
				log.info('Overwriting previous value of project property "%s" with new value "%s" from "%s"'%(name, value, os.path.basename(file)))

			self.properties[name] = value
			log.debug('Setting project property %s="%s" (from %s)', name, self.properties[name], file)

	def expandProperties(self, value, default, name=None):
		"""
		Expand any ${...} project properties or env vars, with ${$} for escaping.
		The "default" is expanded and used if value contains some undefined variables. 
		If default=None then an error is raised instead. If default is a node, its "default" attribute is used
		
		The "name" is used to generate more informative error messages
		"""
		envprefix = self.environment+'.'
		errorprefix = ('Error setting project property "%s": '%name) if name else ''
		
		if hasattr(default, 'getAttribute'):
			default = default.getAttribute("default") if default.hasAttribute("default") else None

		def expandProperty(m):
			m = m.group(1)
			if m == '$': return '$'
			try:
				if m.startswith(envprefix): 
					return os.environ[m[len(envprefix):]]
			except KeyError as ex:
				raise UserError(errorprefix+'cannot find environment variable "%s"'%m[len(envprefix):])
			
			if m in self.properties:
				return self.properties[m]
			else:
				raise UserError(errorprefix+'PySys project property ${%s} is not defined, please check your pysysproject.xml file"'%m)
		try:
			return re.sub(r'[$][{]([^}]+)[}]', expandProperty, value)
		except UserError:
			if default is None: raise
			log.debug('Failed to resolve value "%s" of property "%s", so falling back to default value', value, name or '<unknown>')
			return re.sub(r'[$][{]([^}]+)[}]', expandProperty, default)

	def getRunnerDetails(self):
		nodes = self.root.getElementsByTagName('runner')
		if not nodes: return DEFAULT_RUNNER
		classname, propertiesdict = self._parseClassAndConfigDict(nodes[0], None, returnClassAsName=True)
		assert not propertiesdict, 'Properties are not supported under <runner>'
		return classname

	def getCollectTestOutputDetails(self):
		r = []
		for n in self.root.getElementsByTagName('collect-test-output'):
			x = {
				'pattern':n.getAttribute('pattern'),
				'outputDir':self.expandProperties(n.getAttribute('outputDir'), default=None, name='collect-test-output outputDir'),
				'outputPattern':n.getAttribute('outputPattern'),
			}
			assert 'pattern' in x, x
			assert 'outputDir' in x, x
			assert 'outputPattern' in x, x
			assert '@UNIQUE@' in x['outputPattern'], 'collect-test-output outputPattern must include @UNIQUE@'
			r.append(x)
		return r


	def getPerformanceReporterDetails(self):
		nodeList = self.root.getElementsByTagName('performance-reporter')
		cls, optionsDict = self._parseClassAndConfigDict(nodeList[0] if nodeList else None, 'pysys.utils.perfreporter.CSVPerformanceReporter')
			
		summaryfile = optionsDict.pop('summaryfile', '')
		summaryfile = self.expandProperties(summaryfile, default=None, name='performance-reporter summaryfile')
		if optionsDict: raise UserError('Unexpected performancereporter attribute(s): '+', '.join(list(optionsDict.keys())))
		
		return cls, summaryfile

	def getProjectHelp(self):
		help = ''
		for e in self.root.getElementsByTagName('project-help'):
			for n in e.childNodes:
				if (n.nodeType in {e.TEXT_NODE,e.CDATA_SECTION_NODE}) and n.data:
					help += n.data
		return help

	def getDescriptorLoaderClass(self):
		nodeList = self.root.getElementsByTagName('descriptor-loader')
		cls, optionsDict = self._parseClassAndConfigDict(nodeList[0] if nodeList else None, 'pysys.xml.descriptor.DescriptorLoader')
		
		if optionsDict: raise UserError('Unexpected descriptor-loader attribute(s): '+', '.join(list(optionsDict.keys())))
		
		return cls

	def getTestPlugins(self):
		plugins = []
		for node in self.root.getElementsByTagName('test-plugin'):
			cls, optionsDict = self._parseClassAndConfigDict(node, None)
			alias = optionsDict.pop('alias', None)
			plugins.append( (cls, alias, optionsDict) )
		return plugins
		
	def getRunnerPlugins(self):
		plugins = []
		for node in self.root.getElementsByTagName('runner-plugin'):
			cls, optionsDict = self._parseClassAndConfigDict(node, None)
			alias = optionsDict.pop('alias', None)
			plugins.append( (cls, alias, optionsDict) )
		return plugins

	def getDescriptorLoaderPlugins(self):
		plugins = []
		for node in self.root.getElementsByTagName('descriptor-loader-plugin'):
			cls, optionsDict = self._parseClassAndConfigDict(node, None)
			plugins.append( (cls, optionsDict) )
		return plugins

	def getMakerDetails(self):
		nodes = self.root.getElementsByTagName('maker')
		if not nodes: return DEFAULT_MAKER
		classname, propertiesdict = self._parseClassAndConfigDict(nodes[0], None, returnClassAsName=True)
		assert not propertiesdict, 'Properties are not supported under <maker>'
		return classname
	
	def createFormatters(self):
		stdout = runlog = None
		
		formattersNodeList = self.root.getElementsByTagName('formatters')
		if formattersNodeList:
			formattersNodeList = formattersNodeList[0].getElementsByTagName('formatter')
		if formattersNodeList:
			for formatterNode in formattersNodeList:
				fname = formatterNode.getAttribute('name')
				if fname not in ['stdout', 'runlog']:
					raise UserError('Formatter "%s" is invalid - must be stdout or runlog'%fname)

				if fname == 'stdout':
					cls, options = self._parseClassAndConfigDict(formatterNode, 'pysys.utils.logutils.ColorLogFormatter')
					options['__formatterName'] = 'stdout'
					stdout = cls(options)
				else:
					cls, options = self._parseClassAndConfigDict(formatterNode, 'pysys.utils.logutils.BaseLogFormatter')
					options['__formatterName'] = 'runlog'
					runlog = cls(options)
		return stdout, runlog

	def getDefaultFileEncodings(self):
		result = []
		for n in self.root.getElementsByTagName('default-file-encoding'):
			pattern = (n.getAttribute('pattern') or '').strip().replace('\\','/')
			encoding = (n.getAttribute('encoding') or '').strip()
			if not pattern: raise UserError('<default-file-encoding> element must include both a pattern= attribute')
			if encoding: 
				codecs.lookup(encoding) # give an exception if an invalid encoding is specified
			else:
				encoding=None
			result.append({'pattern':pattern, 'encoding':encoding})
		return result

	def getExecutionOrderHints(self):
		result = []
		secondaryModesHintDelta = None
		
		def makeregex(s):
			if not s: return None
			if s.startswith('!'): raise UserError('Exclusions such as !xxx are not permitted in execution-order configuration')
			
			# make a regex that will match either the entire expression as a literal 
			# or the entire expression as a regex
			s = s.rstrip('$')
			try:
				#return re.compile('(%s|%s)$'%(re.escape(s), s))
				return re.compile('%s$'%(s))
			except Exception as ex:
				raise UserError('Invalid regular expression in execution-order "%s": %s'%(s, ex))
		
		for parent in self.root.getElementsByTagName('execution-order'):
			if parent.getAttribute('secondaryModesHintDelta'):
				secondaryModesHintDelta = float(parent.getAttribute('secondaryModesHintDelta'))
			for n in parent.getElementsByTagName('execution-order'):
				moderegex = makeregex(n.getAttribute('forMode'))
				groupregex = makeregex(n.getAttribute('forGroup'))
				if not (moderegex or groupregex): raise UserError('Must specify either forMode, forGroup or both')
				
				hintmatcher = lambda groups, mode, moderegex=moderegex, groupregex=groupregex: (
					(moderegex is None or moderegex.match(mode or '')) and
					(groupregex is None or any(groupregex.match(group) for group in groups))
					)
				
				result.append( 
					(float(n.getAttribute('hint')), hintmatcher )
					)
		if secondaryModesHintDelta is None: 
			secondaryModesHintDelta = +100.0 # default value
		return result, secondaryModesHintDelta

	def getWriterDetails(self):
		writersNodeList = self.root.getElementsByTagName('writers')
		if writersNodeList == []: return []
		
		writers = []
		writerNodeList = writersNodeList[0].getElementsByTagName('writer')
		if not writerNodeList: return []
		for writerNode in writerNodeList:
			pythonclassconstructor, propertiesdict = self._parseClassAndConfigDict(writerNode, None)
			writers.append( (pythonclassconstructor, propertiesdict) )
		return writers

	def addToPath(self):		
		for elementname in ['path', 'pythonpath']:
			pathNodeList = self.root.getElementsByTagName(elementname)

			for pathNode in pathNodeList:
					value = self.expandProperties(pathNode.getAttribute("value"), default=None, name='pythonpath')
					relative = pathNode.getAttribute("relative")
					if not value: 
						raise UserError('Cannot add directory to the pythonpath: "%s"'%value)

					if relative == "true": value = os.path.join(self.dirname, value)
					value = os.path.normpath(value)
					if not os.path.isdir(value): 
						raise UserError('Cannot add non-existent directory to the python <path>: "%s"'%value)
					else:
						log.debug('Adding value to path: %s', value)
						sys.path.append(value)


	def writeXml(self):
		f = open(self.xmlfile, 'w')
		f.write(self.doc.toxml())
		f.close()


	def _parseClassAndConfigDict(self, node, defaultClass, returnClassAsName=False):
		"""Parses a dictionary of arbitrary options and a python class out of the specified XML node.

		The node may optionally contain classname and module (if not specified as a separate attribute,
		module will be extracted from the first part of classname); any other attributes will be returned in
		the optionsDict, as will <property name=""></property> child elements.

		:param node: The node, may be None
		:param defaultClass: a string specifying the default fully-qualified class
		:return: a tuple of (pythonclassconstructor, propertiesdict), or if returnClassAsName (classname, propertiesDict)
		"""
		optionsDict = {}
		if node:
			for att in range(node.attributes.length):
				name = node.attributes.item(att).name.strip()
				optionsDict[name] = self.expandProperties(node.attributes.item(att).value, default=None, name=name)
			for tag in node.getElementsByTagName('property'):
				assert tag.getAttribute('name')
				optionsDict[tag.getAttribute('name')] = self.expandProperties(tag.getAttribute("value"), default=tag, name=tag.getAttribute('name'))
		classname = optionsDict.pop('classname', defaultClass)
		mod = optionsDict.pop('module', '.'.join(classname.split('.')[:-1]))
		classname = classname.split('.')[-1]

		if returnClassAsName:
			return (mod+'.'+classname).strip('.'), optionsDict

		# defer importing the module until we actually need to instantiate the 
		# class, to avoid introducing tricky module import order problems, given 
		# that the project itself needs loading very early
		def classConstructor(*args, **kwargs):
			module = import_module(mod, sys.path)
			cls = getattr(module, classname)
			return cls(*args, **kwargs) # invoke the constructor for this class
		return classConstructor, optionsDict

def getProjectConfigTemplates():
	"""Get a list of available templates that can be used for creating a new project configuration. 
	
	:return: A dict, where each value is an absolute path to an XML template file 
		and each key is the display name for that template. 
	"""
	templatedir = os.path.dirname(__file__)+'/templates/project'
	templates = { t.replace('.xml',''): templatedir+'/'+t 
		for t in os.listdir(templatedir) if t.endswith('.xml')}
	assert templates, 'No project templates found in %s'%templatedir
	return templates

def createProjectConfig(targetdir, templatepath=None):
	"""Create a new project configuration file in the specified targetdir. 
	"""
	if not templatepath: templatepath = getProjectConfigTemplates()['default']
	mkdir(targetdir)
	# using ascii ensures we don't unintentionally add weird characters to the default (utf-8) file
	with openfile(templatepath, encoding='ascii') as src:
		with openfile(os.path.abspath(targetdir+'/'+DEFAULT_PROJECTFILE[0]), 'w', encoding='ascii') as target:
			for l in src:
				l = l.replace('@PYTHON_VERSION@', '%s.%s.%s'%sys.version_info[0:3])
				l = l.replace('@PYSYS_VERSION@', '.'.join(__version__.split('.')[0:3]))
				target.write(l)

class Project(object):
	"""Contains settings for the entire test project, as defined by the 
	``pysysproject.xml`` project configuration file.
	
	To get a reference to the current `Project` instance, use the 
	`pysys.basetest.BaseTest.project` 
	(or `pysys.process.user.ProcessUser.project`) field. 
	
	All project properties are strings. If you need to get a project property value that's a a bool/int/float it is 
	recommended to use `getProperty()` which will automatically perform the conversion. For string properties 
	you can just use ``project.propName`` or ``project.properties['propName']``. 
	
	:ivar dict(str,str) ~.properties: The resolved values of all project properties defined in the configuration file. 
		In addition, each of these is set as an attribute onto the `Project` instance itself. 
	:ivar str ~.root: Full path to the project root directory, as specified by the first PySys project
		file encountered when walking up the directory tree from the start directory. 
		If no project file was found, this is just the start directory PySys was run from.
	:ivar str ~.projectFile: Full path to the project file.  
	
	"""
	
	__INSTANCE = None
	__frozen = False
	
	def __init__(self, root, projectFile, outdir=None):
		self.root = root
		if not outdir: outdir = DEFAULT_OUTDIR

		if projectFile is None: # very old legacy behaviour
			self.startTimestamp = time.time()
			self.runnerClassname = DEFAULT_RUNNER
			self.makerClassname = DEFAULT_MAKER
			self.writers = []
			self.perfReporterConfig = None
			self.defaultFileEncodings = [] # ordered list where each item is a dictionary with pattern and encoding; first matching item wins
			self.collectTestOutput = []
			self.projectHelp = None
			self.testPlugins = []
			self.runnerPlugins = []
			self._descriptorLoaderPlugins = []
			self.properties = {'outDirName':os.path.basename(outdir)}
			stdoutformatter, runlogformatter = None, None
			self.projectFile = None
		else:
			if not os.path.exists(os.path.join(root, projectFile)):
				raise UserError("Project file not found: %s" % os.path.normpath(os.path.join(root, projectFile)))
			from pysys.xml.project import XMLProjectParser
			try:
				parser = XMLProjectParser(root, projectFile, outdir=outdir)
			except UserError:
				raise
			except Exception as e: 
				raise Exception("Error parsing project file \"%s\": %s" % (os.path.join(root, projectFile),sys.exc_info()[1]))
			else:
				parser.checkVersions()
				self.projectFile = os.path.join(root, projectFile)
				
				self.startTimestamp = parser.startTimestamp
				
				# get the properties
				properties = parser.getProperties()
				keys = list(properties.keys())
				keys.sort()
				for key in keys: 
					if not hasattr(self, key): # don't overwrite existing props; people will have to use .getProperty() to access them
						setattr(self, key, properties[key])
				self.properties = dict(properties)
				
				# add to the python path
				parser.addToPath()
		
				# get the runner if specified
				self.runnerClassname = parser.getRunnerDetails()
		
				# get the maker if specified
				self.makerClassname = parser.getMakerDetails()

				self.writers = parser.getWriterDetails()
				self.testPlugins = parser.getTestPlugins()
				self.runnerPlugins = parser.getRunnerPlugins()
				self._descriptorLoaderPlugins = parser.getDescriptorLoaderPlugins()

				self.perfReporterConfig = parser.getPerformanceReporterDetails()
				
				self.descriptorLoaderClass = parser.getDescriptorLoaderClass()

				# get the stdout and runlog formatters
				stdoutformatter, runlogformatter = parser.createFormatters()
				
				self.defaultFileEncodings = parser.getDefaultFileEncodings()
				
				self.executionOrderHints, self.executionOrderSecondaryModesHintDelta = parser.getExecutionOrderHints()
				
				self.collectTestOutput = parser.getCollectTestOutputDetails()
				
				self.projectHelp = parser.getProjectHelp()
				self.projectHelp = parser.expandProperties(self.projectHelp, default=None, name='project-help')
				
				# set the data attributes
				parser.unlink()
		
		if not stdoutformatter: stdoutformatter = ColorLogFormatter({'__formatterName':'stdout'})
		if not runlogformatter: runlogformatter = BaseLogFormatter({'__formatterName':'runlog'})
		PySysFormatters = collections.namedtuple('PySysFormatters', ['stdout', 'runlog'])
		self.formatters = PySysFormatters(stdoutformatter, runlogformatter)
		
		# for safety (test independence, and thread-safety), make it hard for people to accidentally edit project properties later
		self.properties = makeReadOnlyDict(self.properties)
		self.__frozen = True

	def __setattr__(self, name, value):
		if self.__frozen: raise Exception('Project cannot be modified after it has been loaded (use the runner to store global state if needed)')
		object.__setattr__(self, name, value)

	def expandProperties(self, value):
		"""
		Expand any ${...} project properties in the specified string. 
		
		An exception is thrown if any property is missing. This method is only for expanding project properties so 
		``${env.*}`` syntax is not permitted (if you need to expand an environment variable, use a project property). 
		
		.. versionadded:: 1.6.0
		
		:param str value: The string in which any properties will be expanded. ${$} can be used for escaping a literal $ if needed. 
		:return str: The value with properties expanded, or None if value=None. 
		"""
		if not value: return value
		return re.sub(r'[$][{]([^}]+)[}]', 
			lambda m: '$' if m.group(1)=='$' else self.properties[m.group(1)], value)

	def getProperty(self, key, default):
		"""
		Get the specified project property value, or a default if it is not defined, with type conversion from string 
		to int/float/bool (matching the default's type). 

		.. versionadded:: 1.6.0
		
		:param str key: The name of the property.
		:param bool/int/float/str default: The default value to return if the property is not set or is an empty string. 
			The type of the default parameter will be used to convert the property value from a string if it is 
			provided. An exception will be raised if the value is non-empty but cannot be converted to the indicated type. 
		"""
		if not self.properties.get(key): return default
		val = self.properties[key]
		if default is True or default is False:
			if val.lower()=='true': return True
			if val.lower()=='false': return False
			raise Exception('Unexpected value for boolean project property %s=%s'%(key, val))
		elif isinstance(default, int):
			return int(val)
		elif isinstance(default, float):
			return float(val)
		elif isinstance(default, str):
			return val # nothing to do
		else:
			raise Exception('Unsupported type for "%s" property default: %s'%(key, type(default).__name__))

	@staticmethod
	def getInstance():
		"""
		Provides access to the singleton instance of Project.
		
		Raises an exception if the project has not yet been loaded.  
		
		Use ``self.project`` to get access to the project instance where possible, 
		for example from a `pysys.basetest.BaseTest` or `pysys.baserunner.BaseRunner` class. This attribute is for 
		use in internal functions and classes that do not have a ``self.project``.
		"""
		if Project.__INSTANCE: return Project.__INSTANCE
		if 'doctest' in sys.argv[0]: return None # special-case for doctesting
		raise Exception('Cannot call Project.getInstance() as the project has not been loaded yet')
	
	@staticmethod
	def findAndLoadProject(startdir=None, outdir=None):
		"""Find and load a project file, starting from the specified directory. 
		
		If this fails an error is logged and the process is terminated. 
		
		The method walks up the directory tree from the supplied path until the 
		PySys project file is found. The location of the project file defines
		the project root location. The contents of the project file determine 
		project specific constants as specified by property elements in the 
		xml project file.
		
		To ensure that all loaded modules have a pre-initialised projects 
		instance, any launching application should first import the loadproject
		file, and then make a call to it prior to importing all names within the
		constants module.

		:param st rstartdir: The initial path to start from when trying to locate the project file
		:param str outdir: The output directory specified on the command line. Some project properties may depend on 
			this. 

		"""
		projectFile = os.getenv('PYSYS_PROJECTFILE', None)
		search = startdir or os.getcwd()
		if not projectFile:
			projectFileSet = set(DEFAULT_PROJECTFILE)
			
			drive, path = os.path.splitdrive(search)
			while (not search == drive):
				intersection =  projectFileSet & set(os.listdir(search))
				if intersection : 
					projectFile = intersection.pop()
					break
				else:
					search, drop = os.path.split(search)
					if not drop: search = drive
		
			if not (projectFile is not None and os.path.exists(os.path.join(search, projectFile))): # pragma: no cover
				if os.getenv('PYSYS_PERMIT_NO_PROJECTFILE','').lower()=='true':
					sys.stderr.write("WARNING: No project file found; using default settings and taking project root to be '%s' \n" % (search or '.'))
				else:
					sys.stderr.write('\n'.join([
						#                                                                               |
						"WARNING: No PySys test project file exists in this directory (or its parents):",
						"  - If you wish to start a new project, begin by running 'pysys makeproject'.",
						"  - If you are trying to use an existing project, change directory to a ",
						"    location under the root test directory that contains your project file.",
						"  - If you wish to use an existing project that has no configuration file, ",
						"    set the PYSYS_PERMIT_NO_PROJECTFILE=true environment variable.",
						""
					]))
					sys.exit(1)

		try:
			project = Project(search, projectFile, outdir=outdir)
			stdoutHandler.setFormatter(project.formatters.stdout)
			import pysys.constants
			pysys.constants.PROJECT = project # for compatibility for old tests
			Project.__INSTANCE = project # set singleton
			return project
		except UserError as e: 
			sys.stderr.write("ERROR: Failed to load project - %s"%e)
			sys.exit(1)
		except Exception as e:
			sys.stderr.write("ERROR: Failed to load project due to %s - %s\n"%(e.__class__.__name__, e))
			traceback.print_exc()
			sys.exit(1)
