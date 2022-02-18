import glob
from setuptools import setup, find_packages


setup(
   name='alertaggregation',
   version='0.1',
   description='Aggregates AMiner alerts',
   author='AIT',
   author_email='name.surname@ait.ac.at',
   packages=find_packages(),
   include_package_data=True,
   data_files=[
      ('alertaggregation/data/aminer', glob.glob('alertaggregation/data/aminer/*', recursive=True)),
      ('alertaggregation/data/ossec', glob.glob('alertaggregation/data/ossec/*', recursive=True))
   ],
   install_requires=['cdifflib', 'editdistance'], # dependencies
   python_requires='>=3.7',
)
