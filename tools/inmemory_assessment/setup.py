#!/usr/bin/env python3

from pathlib import Path
from setuptools import setup, find_packages

_metadata: dict[str, str] = {}
metadata_path = Path(__file__).parent / "inmemory_assessment_metadata.py"
with metadata_path.open("r", encoding="utf-8") as metadata_file:
    exec(metadata_file.read(), _metadata)

# Read requirements from requirements.txt
with open('requirements.txt', 'r') as f:
    requirements = [line.strip() for line in f if line.strip() and not line.startswith('#')]

# Read README for long description
with open('README.md', 'r', encoding='utf-8') as f:
    long_description = f.read()

setup(
    name=_metadata["APP_NAME"],
    version=_metadata["VERSION"],
    description='A workload assessment tool for Valkey and Redis OSS clusters.',
    long_description=long_description,
    long_description_content_type='text/markdown',
    author='AWS In-Memory Team',
    url='https://github.com/aws-samples/amazon-elasticache-samples/tree/main/tools/inmemory_assessment',
    project_urls={
        'Source': 'https://github.com/aws-samples/amazon-elasticache-samples/tree/main/tools/inmemory_assessment',
        'Bug Reports': 'https://github.com/aws-samples/amazon-elasticache-samples/issues',
        'Documentation': 'https://github.com/aws-samples/amazon-elasticache-samples/blob/main/tools/inmemory_assessment/README.md',
    },
    license='MIT',
    py_modules=['inmemory_assessment', 'inmemory_assessment_metadata'],
    python_requires='>=3.8',
    install_requires=requirements,
    entry_points={
        'console_scripts': [
            f"{_metadata['APP_NAME']}=inmemory_assessment:app",
        ],
    },
    classifiers=[
        'Development Status :: 5 - Production/Stable',
        'Intended Audience :: Developers',
        'Intended Audience :: System Administrators', 
        'License :: OSI Approved :: MIT License',
        'Operating System :: OS Independent',
        'Programming Language :: Python :: 3',
        'Programming Language :: Python :: 3.8',
        'Programming Language :: Python :: 3.9',
        'Programming Language :: Python :: 3.10',
        'Programming Language :: Python :: 3.11',
        'Programming Language :: Python :: 3.12',
        'Topic :: Database',
        'Topic :: System :: Systems Administration',
    ],
    keywords='inmemory in-memory workload assessment valkey redis oss elasticache aws memorydb metrics',

)