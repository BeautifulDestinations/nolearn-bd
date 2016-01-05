import os
fileDir = os.path.realpath( 'nolearn_global_var.py' )
HOME = fileDir.partition( 'nolearn-bd/nolearn/lasagne' )[0] + 'theano_playground/'
