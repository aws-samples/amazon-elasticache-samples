# Developing AgenticShoppingDemo

Put your notes on developing for/contributing to this package here.



## Build system (Hatch)

This uses the [hatch](https://hatch.pypa.io/latest/) build system and running
`brazil-build <command>` directly forwards to `hatch <command` after setting
up a venv for the specific hatch version and python runtime at `.hatch`.

After an initial build, you can activate the hatch-environment and
use it directly by running `source .hatch/bin/activate` or the equivalent
activate script for your shell of choice. In addition, after the initial build, 
you can use hatch directly.

A number of scripts and commands exist in `pyproject.toml` under the `scripts`
configurations with more documentation in the comments of `pyproject.toml`.
Running a script for a specific environment is simply running 
`hatch run <env_name>:<script>`.  You can omit the `<env_name>` for those under
the `default` environment. 


Here are some ways you can use hatch after running an initial `brazil-build`.

`hatch run release`
This is the release command. It runs scripts to check typing (using mypy), and it 
also runs tests and coverage for those tests. 

`hatch test`
This is the test command. It runs the tests and runs coverage on those tests. 

`hatch typing`
This is the command to run typing. It runs mypy by default. 

`hatch fmt`
Formats your code using ruff.

### Using PeruHatch

This template uses `PeruHatch`. `PeruHatch` comes with a build script, `peru-hatch` that installs Hatch, downloads a plugin that configures a Hatch environment, and verifies that Hatch and the plugin are configured correctly. 

#### Using PeruPython:

* https://builderhub.corp.amazon.com/docs/brazil/user-guide/python-peru.html
