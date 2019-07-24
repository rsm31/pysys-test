from pysys.constants import *
from pysys.basetest import BaseTest

class PySysTest(BaseTest):
	def execute(self):
		pass

	def validate(self):
		self.assertGrep(file='file.txt', filedir=self.input, expr='moon shines bright')
		
		self.log.info('expected failure:')
		self.assertGrep(file='file.txt', filedir=self.input, expr='moon shines r.ght')
		self.checkForFailedOutcome()

		self.log.info('expected failure:')
		self.assertGrep(file='file.txt', filedir=self.input, expr='moon [^ ]*', contains=False)
		self.checkForFailedOutcome()

		self.log.info('expected failure:')
		self.assertGrep(file='file.txt', filedir=self.input, expr='moo. [^ ]', contains=False)
		self.checkForFailedOutcome()

		self.log.info('expected failure:')
		self.assertGrep(file='file.txt', filedir=self.input, expr='ERROR', contains=False)
		self.checkForFailedOutcome()

		self.log.info('expected failure:')
		self.assertGrep(file='file.txt', filedir=self.input, expr=' WARN .*', contains=False)
		self.checkForFailedOutcome()

		# check for correct failure message:
		self.log.info('')
		self.assertGrep(file='run.log', expr='Grep on file.txt contains "moon shines r[.]ght" ... failed')
		# for an expression ending in *, print just the match
		self.assertGrep(file='run.log', expr='Grep on file.txt does not contain "moon [^ ]*" failed with: "moon shines" ... failed', literal=True)
		# for an expression not ending in *, print the whole line
		self.assertGrep(file='run.log', expr='Grep on file.txt does not contain "moo. [^ ]" failed with: "And the moon shines bright as I rove at night," ... failed', literal=True)
		# here's a real-world example of why that's useful
		self.assertGrep(file='run.log', expr='Grep on file.txt does not contain "ERROR" failed with: "2019-07-24 [Thread1] ERROR This is an error message!"', literal=True)
		self.assertGrep(file='run.log', expr='Grep on file.txt does not contain " WARN .*" failed with: " WARN This is a warning message!"', literal=True)
		
		self.log.info('')
		self.assertGrep(file='file.txt', filedir=self.input, expr='moon shines right', contains=False)
		self.assertGrep(file='file.txt', filedir=self.input, expr='(?P<tag>moon) shines bright')
		self.assertGrep(file='file.txt', filedir=self.input, expr='moon.*bright')
		self.assertGrep(file='file.txt', filedir=self.input, expr='moon.*bright', ignores=['oon'], contains=False)
		self.assertGrep(file='file.txt', filedir=self.input, expr='moon.*bright', ignores=['pysys is great', 'oh yes it is'])
		self.assertGrep(file='file.txt', filedir=self.input, expr='Now eastlin|westlin winds')
		
	def checkForFailedOutcome(self):
		outcome = self.outcome.pop()
		if outcome == FAILED: self.addOutcome(PASSED)
		else: self.addOutcome(FAILED)
		
