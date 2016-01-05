import os
import inspect

class DummyClass: pass

moduleDir =  os.path.dirname(os.path.abspath(inspect.getsourcefile(DummyClass)))
HOME = moduleDir.partition( 'nolearn-bd/nolearn/lasagne' )[0] + 'theano_playground/'
