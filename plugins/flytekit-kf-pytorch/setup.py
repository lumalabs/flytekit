from setuptools import setup

PLUGIN_NAME = "kfpytorch"

microlib_name = f"flytekitplugins-{PLUGIN_NAME}"

plugin_requires = ["cloudpickle", "flyteidl", "flytekit", "kubernetes"]

__version__ = "0.0.0+develop"

setup(
    title="Kubeflow PyTorch",
    title_expanded="Flytekit Kubeflow PyTorch Plugin",
    name=microlib_name,
    version=__version__,
    author="flyteorg",
    author_email="admin@flyte.org",
    description="K8s based Pytorch plugin for Flytekit",
    namespace_packages=["flytekitplugins"],
    packages=[f"flytekitplugins.{PLUGIN_NAME}"],
    install_requires=plugin_requires,
    extras_require={
        "elastic": ["torch>=1.9.0"],
    },
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
)
