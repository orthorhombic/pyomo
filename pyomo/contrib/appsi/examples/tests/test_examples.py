from pyomo.contrib.appsi.examples import getting_started
from pyomo.common import unittest
import pyomo.environ as pe
from pyomo.contrib.appsi.cmodel import cmodel_available
from pyomo.contrib import appsi


@unittest.skipUnless(cmodel_available, 'appsi extensions are not available')
class TestExamples(unittest.TestCase):
    def test_getting_started(self):
        try:
            import numpy as np
        except:
            raise unittest.SkipTest('numpy is not available')
        opt = appsi.solvers.Ipopt()
        if not opt.available():
            raise unittest.SkipTest('ipopt is not available')
        getting_started.main(plot=False, n_points=10)
