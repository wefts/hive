from groups import evaluate, match_leaf


def axes(**kw):
    base = {"org": "", "team": "", "family": "", "status": "", "site": "", "title": "", "uid": ""}
    base.update(kw)
    return base


def test_leaf_glob_case_insensitive():
    assert match_leaf("CountryX / DivisionA / ops", "*ops*")
    assert match_leaf("DIVISION A", "division a")
    assert not match_leaf("developer", "designer")


def test_all_any_not_and_list_or():
    g = {
        "rule": {
            "all": [
                {"org": "division a"},
                {"any": [{"team": "*ops*"}, {"family": ["devops", "sysadmin"]}]},
            ]
        },
        "include": [],
        "exclude": [],
    }
    assert evaluate(g, axes(org="division a", team="countryx / division a / ops", family="developer"))
    assert evaluate(g, axes(org="division a", family="sysadmin"))
    assert not evaluate(g, axes(org="division b", family="sysadmin"))  # wrong org
    assert not evaluate(g, axes(org="division a", family="developer"))  # neither ops nor devops/sysadmin


def test_include_exclude_override_rule():
    g = {"rule": {"any": [{"family": ["developer"]}]}, "include": ["u_in"], "exclude": ["u_out"]}
    assert evaluate(g, axes(uid="u_in", family="hr"))        # include wins over failing rule
    assert not evaluate(g, axes(uid="u_out", family="developer"))  # exclude wins over passing rule
    assert evaluate(g, axes(uid="normal", family="developer"))


def test_explicit_only_group_no_rule():
    g = {"include": ["a", "b"], "exclude": []}
    assert evaluate(g, axes(uid="a"))
    assert not evaluate(g, axes(uid="z"))
