"""One stdlib-only missing-parent preparation literal, before SDK checkout."""
from pathlib import Path
import shlex
import seed_transport

SCRIPT = '''import os
from pathlib import Path
import stat
import sys
if os.name == "nt" and sys.version_info < (3, 13):
    raise SystemExit("Windows private SDK directories require the selected Python 3.13 or newer")
parent = Path.cwd() / ".artifacts"
for candidate in [parent, *parent.parents]:
    try:
        info = candidate.lstat()
    except FileNotFoundError:
        continue
    if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
        raise SystemExit("SDK artifact parent must not traverse links or reparse points")
if parent.exists():
    if not parent.is_dir():
        raise SystemExit("SDK artifact parent must be a directory")
else:
    parent.mkdir(mode=0o700)
'''


def argument():
    """Keep native Windows -c transport finite and on one physical line."""
    return 'exec(' + repr(SCRIPT) + ')'


def shell(executable):
    return executable + ' -I -B -c ' + shlex.quote(argument())


# Shared exact caller-Python selection, before any downloaded SDK code.
SELECTION = r'''version = caller.get("tools", {}).get("python")
version = version.get("version") if isinstance(version, dict) else version
if not isinstance(version, str) or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version):
    raise SystemExit("bootstrap Python requires the exact caller-pinned version")
environment = dict(os.environ, MISE_AUTO_INSTALL="false", MISE_EXEC_AUTO_INSTALL="false", MISE_TASK_RUN_AUTO_INSTALL="false")
install = subprocess.check_output(["mise", "where", "python@" + version], cwd=root,
    env=environment, text=True, timeout=30).strip()
directory = Path(install)
if not directory.is_absolute() or not directory.is_dir():
    raise SystemExit("exact bootstrap Python is absent; run explicit setup")
candidates = [directory / "bin/python3", directory / "python.exe"]
interpreter = next((path.resolve() for path in candidates if path.is_file()
    and path.resolve().is_relative_to(directory.resolve())), None)
if interpreter is None:
    raise SystemExit("exact bootstrap Python executable is absent; run explicit setup")
actual = subprocess.check_output([str(interpreter), "-I", "-B", "-c",
    "import sys; print('.'.join(map(str,sys.version_info[:3])))"], cwd=root,
    env=environment, text=True, timeout=30).strip()
if actual != version:
    raise SystemExit("bootstrap Python differs from the exact caller pin")
'''

PINNED_SEED = Path(__file__).with_name("sdk_source.py").read_text() + r'''
import sys
root = Path.cwd().resolve()
caller = tomllib.loads(read_regular(root / "mise.toml").decode().replace("\r\n", "\n").replace("\r", "\n"))
''' + SELECTION + r'''subprocess.run([str(interpreter), "-I", "-B", "-c", 'import os\nfrom pathlib import Path\nimport stat\nimport sys\nif os.name == "nt" and sys.version_info < (3, 13):\n    raise SystemExit("Windows private SDK directories require the selected Python 3.13 or newer")\nparent = Path.cwd() / ".artifacts"\nfor candidate in [parent, *parent.parents]:\n    try:\n        info = candidate.lstat()\n    except FileNotFoundError:\n        continue\n    if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:\n        raise SystemExit("SDK artifact parent must not traverse links or reparse points")\nif parent.exists():\n    if not parent.is_dir():\n        raise SystemExit("SDK artifact parent must be a directory")\nelse:\n    parent.mkdir(mode=0o700)\n'], cwd=root, env=environment, check=True)
'''


# Fixed authoring-time payload; runtime rendering never compresses.
SEED_LENGTH = 7963
SEED_SHA256 = 'dc485ada54199739c780ae9fe030ccde29f10396a09270897f4db590ad005c1d'
ENCODED_SEED = (
    'eNrVWVtz2zYWftevQPmwJBOJtuvMbtddbeskTpuJa3fipDfbw6EoyEJDAVoCtK2m+e/7HQC8SfKl3dmZXT/I5MHBwcG5fgCDIPiB'
    'l1ooyfbYNDPZSMlixc5evmFToXN1zcvVl0xnM86MYgtRlqpkEz5TJWeFyqZCXrFMEn8SBMFALJaqNEzpwaxUC7bMzLwQE+bJ3+O1'
    'Zil5/aRNZprnarIsVc61rilGLQqIGAy+O/wpPTt9//bFERuzz//6+d6zZ4Ozbw/xUvIkV4ulKHhUhufZaLY7+vvlx2e7ny5+CeMB'
    'VEuPTt69/RmcYRh6sV6zISsruVwNmV7pAZemXIHLjyWkboSBJCuvrs/3LuNB/QKmln5waek0KxFS89JEuxBoysgKxECJhzge2KUS'
    '/KZLK7nmiK0WqcwWfByk6SITMk2DeABtB4PBlM/YcmXmSqaWO+K3PK9MNin4kFnKkD2BJtUCLzo+GDD8wRfPK1FMmdCqyAyfMqt2'
    'NjO8ZGbOWZ4VBR7nmWZwsZgJsJi50NbzWlVlzq1DSVjJTVVKdm4VbhaH1uHodUi/z+1vjt/G2B0DxF39Lv2OxBRvwqwit5ZXW2BA'
    'w4Ymkzn3Q0M2WRleb4z+HJ184PSc8lxNeRRWZjb6Ah73kqQy26RBr5ghiAsuPS3h0gqIY/ZP1gkzMIUXuyET0q/UqlBmQnP2Q1ZU'
    '/IhSIgrJbvWmYLF/VaLkmk1UJacw7ft3r0ZfsO9okhPl1bwmCRpb8WGeUE7pWq+SL4sMqocX5YUkG+M37lJrWpxccROF11mpQfr4'
    'qbFBtGYEt94QuZ0bawWzWtZUJ8MoVeg0V/BclhuIxnRnSmma7dMfJrt55+tzLtlnY1QTMGxdvbtOyWdh7HyyLpzmIr+TWVUUi8zk'
    '86i/HE29jOOHfOKDpfGIkpwinioe/CIWCxfNzje1A713fOB/DP2E8IDtweJwgNLCqHIFQigFBY/esVqFdvhaeO4tGn/yCSBVWgj5'
    'QUdUC/wu6BGxYOuOJSfZBPlbGcRm7dFMrpwhEqFTvVqQkChms9odFK3nNBlZZ0uSKz/68rGWWlTaWOPDmbRv1HnSs28Skuw3kmpx'
    'JTNQeSTkTGHZIctLpfX4VVboOrNh+qow2JxlSrRJp/x6yOoXIVX7soA92zdJyw970cEYQigzKDBuxRBslZjC+CfwatzO1eK3jqSF'
    'EQueSt3biVcMlrVKMw6dWfTEkdu5eTPX7brk2RQOvaqKrNziwr537Yhvmq69JAV1vahXq4iSnKWvz94efRM57toaNlVbEm2rV6oe'
    'cm3Tyjcrk9/DltqEoM5LsUScQ2mlE7VExXSRhbfT9O3L05Pjn9nvjTMUCkt4mp6cvjo9Pj79Ee7YjbcNnzw/Pn3xxg67lW4EjAaZ'
    's6ldo12YsmkSxgxdar0CEyfUt5rNrDF90ZwBB0gVxW1BIVjTdgvyXNSp8k/ZXsuaF0o/Qqrro9tdGVFjoSU3u0knVZwzY6qUHaqV'
    'u1EJOwxu1+vTnNb3zXPL1Yn5rqw2lnaSexzxo/udLx35PJNXMN/NHNayKQJ42C8cZJhuCrmZUamUaSqFz8tOgtmKaHnYDgsX0CWh'
    'hhnW2QgEADY+/R9GE/9/2OE+6HDfbqymECeor2rXTya17jYA+iExQXOMNHRwK8TsL+i4a5ACurY99FPtdicybbDkZhS1Q+vBVsso'
    'VJ4ZdGtLBRD3zRuerZbLArB4bHvKwR1W6fD7SNjELTXPn8IqLULxo0sh+/Zrek2tManZPNt+1s8fnFuMmMGsNWgBsdHRm+VKmGgK'
    'JXKCOVsOGH7t9ryW5HOef0hVZZaVic5DCLCngpFUIx91IzX5FQJ1c1oIc2poc6U+aNJwHKIYo/ACGUgYr+35Xe6ZXihJ4Gs8I3hh'
    'Zb0I3XGjUbh/5Bgyw2+NrWl4RB+HkuP93RitFG0mqrecZxKScTBKVSmuhHTxeKfn7ahz+0N+dfI8ssIpzzq2WY59I8y31YR1YKXz'
    'r40ed77tBFMYfXUwN2apD3Z2YOR5NTlPLnH+3fkdb193KQe/az13XD36ThydH45+yUa/4aCcJqPLpztr71/FWATMmBV/BeM6S9SG'
    'cHrhWECZQSFPmBQwk84ISN2EfJIk4ScLSmu6nYRDdKIRl6g3O2H837Kbj81mxULd8LJxsz3urrrB3ebwk2FH2ngN2reREG0UgXrO'
    '3UeZNSc+4ICwq8iGvMfYu53etfgfLkHCaDYTtygkyPNrlNOyI3n9vNTYtIuA26Tsma8h0zkGL5Etnmtlh85SI+zI5fm3R4cvQ4tb'
    'ao+tG8bWuXtljEZ6rm5GRi0Lfs0Lan/Ypiqu0bBJcqtWQ15fZF0+wb/KFbXRUsF6RSake61s+/rApyPCj3qcFYUbAOxCMZuObThY'
    'kPSHjmdop3SJw29RxIFZeSapLRAetlUY9c17xGfQeENpdNaZuHLKUAv0egEKjLySJV8owxMnIanKosVCG5XS/fOuaeLO590jN+ZV'
    'nYrZDKdOZu8P742+NWBZb66+MgOjhYSur/PbJYY5QcqZ7ejbGzxkqHLagME1aOFZXOzZwkzc5+2J/7K2UHe5+gLFVstMTvuDnWh+'
    'pKF6FuqEgb3RG9VQeBMn+ML3IOCJh100UF/MrvSAJvjriSS/mUad1Bn4+8R1MNpD8Xb+Dgsa/B7ENdpukWlAeDUYsgC/PWpNiwf+'
    'NgaLuVUtgA1skQ4sgnUEd2kadPn9kxv3L0G8dlDw9PqSzAIoTxtsgQE1dxf/9Yt9cE4X0k8vkv5/6Oon18FnvX620oYvjm6RsAGg'
    'sYHcbMm+t5tp6/KG333+N5sacHktSiUJArGx3QtO4ImnDtl3r8+O0sP3707T1ydn7w6Pj8eBBVSBHzr66ejFfePvDs/epG/fn2zl'
    'iQfWPkVBmXQXRLRxQF69mfPSPjiPfR0ABvp9ALsh0sY2VK2NoP+4s7MHoV23Jdnu4BWLa1f2+lB73VZ7crNN3eUr540NjyH5IRWq'
    'fkmX/JT7hcgFEgpJuQwoceRU4FBkT3LnrbrIk4mQO84k+8GQ9YYcOeG3PLiErQ0vUe/cnYSEQSJ7T9PpbA4ZmDkhg86KsIH7bKFT'
    'alC+11GN6s8nhhJ9zYhrnhoVbemScexv38iwXY08TvyDZmu/MzxoQcyvsnsjTdubwkYlaBqMXlPAjZ7b3zxwwRW0xe5LNBBMiYCu'
    'kl8VWtwiW9IXmyF96vHBmdLl4PnB/iU2H/zHkUp3u24nnzWF6tF14d6eQK0AduqYB3Z8lFVY2HzQu5B3fdG7kJ0Pee3LClOwJ9Qc'
    'C93HYxZIE9jgWrch+weL9odsbz8+uJDbd/yjkFN1o8kr1whd/4HSRaFAJPvCaPeueWH7a22c/WRv3+YzBx4J4gvprsR7rYySqj0X'
    'o9FQxjSZ4i/VS+vHJ+6huVj3Opty5Z8sBqBtjVsJ9S2h4+C3OV8a9go5d6LMK7rosX2+I4CuPoSsuKPQib6+Hj4+eRN1r8odcO5d'
    'iAcYooROiSYmqGjUGXfpamX39tnubmedTVOTaWtLMG+qOz4K0MoWZ4GwRJbAcrH1ujcRvxXa6Kh2qy+5frApqH9KGUDgrK2JtCw1'
    'ai/Lr7D4QAuQjca76m+7u2AKu3m6maO2brjbz8G/AYutRZA='
)


def selected_command():
    source = seed_transport.seed_transport(PINNED_SEED, encoded=ENCODED_SEED, length=SEED_LENGTH, sha256=SEED_SHA256)
    return ('exec env MISE_AUTO_INSTALL=false MISE_EXEC_AUTO_INSTALL=false MISE_TASK_RUN_AUTO_INSTALL=false '
            '"${COOKBOOK_BOOTSTRAP_PYTHON:-python3}" -I -B -c ' + shlex.quote(source))
