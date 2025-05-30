from setuptools import setup

PLUGIN_NAME = "whylogs"

microlib_name = f"flytekitplugins-{PLUGIN_NAME}"

plugin_requires = ["flytekit>=1.3.0b2", "whylogs[viz]>=1.1.16"]

__version__ = "0.0.0+develop"

setup(
    title="whylogs",
    title_expanded="Flytekit whylogs Plugin",
    name=microlib_name,
    version=__version__,
    author="whylabs",
    author_email="support@whylabs.ai",
    description="Enable the use of whylogs profiles to be used in flyte tasks to get aggregate statistics about data.",
    url="https://github.com/flyteorg/flytekit/tree/master/plugins/flytekit-whylogs",
    long_description=open("README.md").read(),
    long_description_content_type="text/markdown",
    namespace_packages=["flytekitplugins"],
    packages=[f"flytekitplugins.{PLUGIN_NAME}"],
    install_requires=plugin_requires,
    license="apache2",
    python_requires=">=3.9",
    classifiers=[
        "Intended Audience :: Science/Research",
        "Intended Audience :: Developers",
        "License :: OSI Approved :: Apache Software License",
        "Programming Language :: Python :: 3.9",
        "Programming Language :: Python :: 3.10",
        "Topic :: Scientific/Engineering",
        "Topic :: Scientific/Engineering :: Artificial Intelligence",
        "Topic :: Software Development",
        "Topic :: Software Development :: Libraries",
        "Topic :: Software Development :: Libraries :: Python Modules",
    ],
    entry_points={"flytekit.plugins": [f"{PLUGIN_NAME}=flytekitplugins.{PLUGIN_NAME}"]},
)
