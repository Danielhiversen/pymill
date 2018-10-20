from setuptools import setup

long_description = None
with open("README.md", 'r') as fp:
    long_description = fp.read()

setup(
    name = 'millheater',
    packages = ['mill'],
    install_requires=['aiohttp>=3.0.6', 'async_timeout>=1.4.0'],
    version='0.2.0',
    description='A python3 library to communicate with Mill',
    long_description=long_description,
    python_requires='>=3.5.3',
    author='Daniel Hoyer Iversen',
    author_email='mail@dahoiv.net',
    url='https://github.com/Danielhiversen/pymill',
    license="MIT",
    classifiers=[
        'Intended Audience :: Developers',
        'Operating System :: OS Independent',
        'Programming Language :: Python',
        'Programming Language :: Python :: 3',
        'Topic :: Home Automation',
        'Topic :: Software Development :: Libraries :: Python Modules'
        ]
)
