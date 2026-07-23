#import setuptools
from setuptools import setup, Extension, find_packages
from distutils.util import convert_path
from pathlib import Path


def get_version():
    # single source of truth: src/fsdb/version.py (exec, never import the package)
    ns = {}
    exec(Path("src/fsdb/version.py").read_text(), ns)
    return ns["__version__"]


setup(
    name='didip_util',
    version=get_version(),
    # Discovered, NOT hand-listed. The hand-written list held only the three top-level packages and
    # silently omitted the `ddp_util.iiif` SUBpackage, so an installed didip_util raised
    # "ImportError: cannot import name 'iiif' from partially initialized module 'ddp_util'" at
    # startup -- while a source checkout worked, because the directory is simply present there.
    # find_packages keeps any future subpackage from having to be remembered.
    packages=find_packages(where='src'),
    include_package_data=True,
    #package_dir={'ddp_util': 'src/ddp_util'},
    package_dir={'':'src'},
    #package_data={'frat': ['resources/*.js', 'resources/*.json', 'resources/*.jinja2']},
    #include_package_data=True,
    #scripts=['bin/ddp_leech_sheets', 'bin/ddp_leech_monasterium', 'bin/ddp_leech_charter'],
    license='GPLv3',
    author='Anguelos Nicolaou et al.',
    author_email='anguelos.nicolaou@gmail.com',
    url='https://zimlab.uni-graz.at/gams/projects/didip/general',
    description="DiDip management codebase",
    long_description_content_type="text/markdown",
    long_description=open('README.md').read(),
    entry_points={
        'console_scripts': [
            'ddp_slice_fsdb = fsdb.slice:main_slice_fsdb_cli',
            'ddpa_static_fsdb_serve = ddp_microservices.static_fsdb:main_launch_fsdb_microservice',
            'ddpa_slicer_serve = ddp_microservices.ddp_slicer:main_launch_slicer_microservice',
            'ddp_gateway = ddp_microservices.gateway:main',
            'ddp_scope_probe = ddp_microservices.scope_probe:main',

            # TODO: re-enable once ddp_scripts is a package (needs __init__.py + listed in
            # packages) and recto.py is finished — see the ddp_offline skill.
            #'ddpa_offline_recto = ddp_scripts.recto:recto_main',
        ],
    },
    keywords=["documents", "diplomatics", "monasterium"],
    classifiers=[
        "Intended Audience :: Science/Research",
        "Programming Language :: Python :: 3.6",
        "Programming Language :: Python :: 3.7",
        "Programming Language :: Python :: 3.8",
        "Topic :: Scientific/Engineering"],
    install_requires=["tqdm", "numpy", "flask", "requests", "Pillow", "bs4", "lxml", "python-magic", "fargv", "furl", "anyascii"],
)
