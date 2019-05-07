from pysys.constants import *
from pysys.basetest import BaseTest
from pysys.exceptions import *
from pysys.utils.pycompat import PY2

class PySysTest(BaseTest):
	def execute(self):
		self.proj = self.project
		self.assertTrue(self.proj.env_user == "Felicity Kendal")
		self.assertTrue(self.proj.env_user_prepend == "append-on-front-Felicity Kendal")
		self.assertTrue(self.proj.env_user_append == "Felicity Kendal-append-on-back")
		self.assertTrue(self.proj.env_default == "default value")
		self.assertTrue(self.proj.env_default_none == "")
		self.assertTrue(self.proj.user_firstname == "Simon")
		self.assertTrue(self.proj.user_lastname == "Smith")
		self.assertTrue(self.proj.user_title == "Professor")
		self.assertTrue(self.proj.user_full == "Professor Simon Smith")

		for p in ['projectbool', 'projectbooloverride', 'cmdlineoverride']:
			self.log.info('getBool %s=%r', p, self.getBool(p))

		self.log.info('getBool %s=%r', 'booldeftrue', self.getBool('bool-not-defined', True))
		self.log.info('getBool %s=%r', 'booldeffalse', self.getBool('bool-not-defined', False))		

	def validate(self):
		pass 
