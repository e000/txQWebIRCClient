from setuptools import setup

setup(
    name = 'txQWebIRCClient',
    version = '0.1-dev',
    license='MIT',
    author='Edgeworth E. Euler',
    author_email = 'e@encyclopediadramatica.ch',
    description = 'A transport for QWebIRC',
    install_requires = [
        'twisted>=10.0.0',
    ],
    platforms = 'any',
    packages = [
        'txQWebIRCClient'
    ],
    package_dir = {
        'txQWebIRCClient': 'src'
    }

)