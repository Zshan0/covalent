import subprocess

import covalent as ct


def test_deps_bash_init():
    cmds = ["pip list", "yum install -y gcc"]
    cmd = "apt install -y build-essential"
    deps = ct.DepsBash(cmds)
    deps_from_str = ct.DepsBash(cmd)
    assert deps.commands == cmds
    assert deps_from_str.commands == [cmd]


def test_bash_deps_apply():
    import tempfile
    from pathlib import Path

    f = tempfile.NamedTemporaryFile(delete=True)
    tmp_path = f.name
    f.close()

    cmds = [f"touch {tmp_path}"]
    deps = ct.DepsBash(cmds)

    serialized_fn, serialized_args, serialized_kwargs = deps.apply()

    fn = serialized_fn.get_deserialized()
    args = serialized_args.get_deserialized()
    kwargs = serialized_kwargs.get_deserialized()
    assert args == [cmds]
    assert kwargs == {}

    fn(*args, **kwargs)

    assert Path(tmp_path).is_file()

    Path(tmp_path).unlink()


def test_bash_deps_serialize():
    import json

    dep = ct.DepsBash(["yum install gcc"])
    json_dep = json.dumps(dep.to_dict())
    new_dep = ct.DepsBash().from_dict(json.loads(json_dep))

    assert new_dep.commands == dep.commands
    assert new_dep.apply_fn == dep.apply_fn
    assert new_dep.apply_args == dep.apply_args
    assert new_dep.apply_kwargs == dep.apply_kwargs


def test_call_deps_init():
    from covalent._workflow.transport import TransportableObject

    def f(x):
        return x * x

    dep = ct.DepsCall(f, args=[5])
    g = dep.apply_fn.get_deserialized()
    args = dep.apply_args.get_deserialized()
    assert args == [5]
    assert g(*args) == f(*args)


def test_call_deps_apply():
    from covalent._workflow.transport import TransportableObject

    def f(x):
        return x * x

    dep = ct.DepsCall(f, args=[5])
    g, args, kwargs = (t.get_deserialized() for t in dep.apply())
    assert args == [5]
    assert kwargs == {}
    assert g(*args) == f(*args)


def call_deps_serialize():
    import json

    def f(x):
        return x * x

    dep = ct.DepsCall(f, args=[5])

    json_dep = json.dumps(dep.to_dict())

    new_dep = ct.DepsCall().from_dict(json.loads(json_dep))
    assert new_dep.apply_fn == dep.apply_fn
    assert new_dep.apply_args == dep.apply_args
    assert new_dep.apply_kwargs == dep.apply_kwargs


def test_deps_pip_init():
    import tempfile
    from pathlib import Path

    pkgs = ["pydash==5.1.0"]
    reqs_contents = "numpy\nscipy\n"

    f = tempfile.NamedTemporaryFile("w", delete=False)
    f.write(reqs_contents)
    reqs_path = f.name

    f.close()

    dep = ct.DepsPip(packages=pkgs, reqs_path=reqs_path)

    assert dep.packages == pkgs
    assert dep.reqs_path == reqs_path
    assert dep.requirements_content == reqs_contents

    Path(reqs_path).unlink()


def test_deps_pip_apply():
    import subprocess
    import tempfile
    from pathlib import Path

    pkgs = ["pydash==5.1.0"]

    dep = ct.DepsPip(packages=pkgs)

    g = dep.apply_fn.get_deserialized()
    apply_args = dep.apply_args.get_deserialized()
    apply_kwargs = dep.apply_kwargs.get_deserialized()

    g(*apply_args, **apply_kwargs)

    import pydash

    assert pydash.__version__ == "5.1.0"

    subprocess.run(
        "pip uninstall -y --no-input pydash",
        shell=True,
        stdin=subprocess.DEVNULL,
        capture_output=True,
    )


def test_deps_pip_apply_requirements_file():
    import subprocess
    import tempfile
    from pathlib import Path

    reqs_content = "pydash==5.1.0\n"
    f = tempfile.NamedTemporaryFile("w", delete=False)
    f.write(reqs_content)
    reqs_path = f.name

    f.close()

    dep = ct.DepsPip(reqs_path=reqs_path)

    g = dep.apply_fn.get_deserialized()
    apply_args = dep.apply_args.get_deserialized()
    apply_kwargs = dep.apply_kwargs.get_deserialized()

    g(*apply_args, **apply_kwargs)

    import pydash

    assert pydash.__version__ == "5.1.0"

    subprocess.run(
        "pip uninstall -y --no-input pydash",
        shell=True,
        stdin=subprocess.DEVNULL,
        capture_output=True,
    )

    Path(reqs_path).unlink()
