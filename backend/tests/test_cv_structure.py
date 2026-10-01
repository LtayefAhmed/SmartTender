"""Reading a dated career timeline out of a CV.

Every rule here was written against text that is actually in the base, and the
two that cost the most were not the regexes — they were the assumptions around
them:

**The corpus is English.** ``EDUCATION`` heads 364 CVs, ``FORMATION`` heads 4.
A French-first extractor would have scored near zero on our own data.

**The extractor fragments a line into tokens.** One entry arrives as
``Company Name`` / ``December 2009`` / ``to`` / ``Current`` / ``Staff
Accountant``. The first version of the noise filter dropped the lone ``to``
line — the very connector the date-range pattern needs — and experience recall
fell from 91% to 11% without a single error being raised. That is the test
below named `test_a_range_split_across_lines_is_still_a_range`, and it exists
because nothing else would have caught it.
"""

from __future__ import annotations

import pytest

from app.services.cv_structure import extract_structure


def _cv(*lines: str) -> str:
    return "\n".join(lines)


class TestTheDateRangeIsTheAnchor:
    def test_a_range_split_across_lines_is_still_a_range(self):
        """The layout that 267 CVs in the base actually use. A rule expecting
        the dates and their label on one line reads nothing at all."""
        structure = extract_structure(
            _cv(
                "Experience",
                "Company Name",
                "",
                "December 2009",
                "",
                "to",
                "Current",
                "",
                "Staff Accountant",
                "Prepares general ledger entries by maintaining and coding records.",
            )
        )

        assert len(structure.experiences) == 1
        role = structure.experiences[0]
        assert role.debut == "December 2009"
        assert role.fin.lower() == "current"
        assert role.poste == "Staff Accountant"

    @pytest.mark.parametrize(
        "start,end",
        [
            ("Jun 2014", "Feb 2016"),
            ("06/2014", "02/2016"),
            ("2014", "2016"),
            ("janvier 2014", "mars 2016"),
        ],
    )
    def test_the_written_forms_are_all_read(self, start, end):
        structure = extract_structure(
            _cv("Experience", "Développeur Java", f"{start} - {end}", "Migration du socle.")
        )

        assert len(structure.experiences) == 1

    def test_an_ongoing_role_is_recognised(self):
        structure = extract_structure(
            _cv("Experience", "Consultant", "2019 to Present", "Conduite du chantier.")
        )

        assert structure.experiences[0].fin.lower() == "present"


class TestTheJobTitleIsFoundByItsWordsNotItsPosition:
    """Measured on real CVs: the title sits above the dates as often as below.
    A positional rule filed `Database Programmer/Analyst` as the *employer*."""

    def test_a_title_above_the_dates_is_read_as_the_title(self):
        structure = extract_structure(
            _cv(
                "Experience",
                "Database Programmer/Analyst",
                "Jun 2014",
                "to",
                "Feb 2016",
                "Built the reporting layer for the finance department.",
            )
        )

        assert structure.experiences[0].poste == "Database Programmer/Analyst"

    def test_a_title_below_the_dates_is_read_as_the_title(self):
        structure = extract_structure(
            _cv(
                "Experience",
                "Company Name",
                "December 2009",
                "to",
                "Current",
                "Staff Accountant",
                "Reconciles accounts and prepares the consolidated statements.",
            )
        )

        assert structure.experiences[0].poste == "Staff Accountant"

    def test_a_bare_year_is_never_a_label(self):
        """A real CV produced `employeur=1983` before this rule: a stray year
        sitting above the dates was read as the company."""
        structure = extract_structure(
            _cv(
                "Experience",
                "1983",
                "1991 - 1995",
                "Credential granted by the state board of education.",
            )
        )

        assert structure.experiences[0].employeur != "1983"


class TestAnonymisationIsNotAFact:
    """459 of the 462 CVs in the base replaced employers with the literal
    string "Company Name". Printing that into a funder's form would put a
    fabricated-looking employer in a contractual document."""

    def test_a_placeholder_employer_becomes_nothing(self):
        structure = extract_structure(
            _cv(
                "Experience",
                "Company Name",
                "2019 - 2022",
                "Software Engineer",
                "Delivered the payments integration.",
            )
        )

        assert structure.experiences[0].employeur is None
        assert structure.anonymised_fields >= 1

    def test_a_placeholder_location_becomes_nothing(self):
        structure = extract_structure(
            _cv(
                "Experience",
                "Software Engineer",
                "2019 - 2022",
                "City",
                "State",
                "Delivered the payments integration.",
            )
        )

        assert structure.experiences[0].lieu is None

    def test_a_real_employer_survives(self):
        structure = extract_structure(
            _cv(
                "Experience",
                "Inetum Tunisie",
                "2019 - 2022",
                "Software Engineer",
                "Delivered the payments integration for a national bank.",
            )
        )

        role = structure.experiences[0]
        assert role.poste == "Software Engineer"
        assert role.employeur == "Inetum Tunisie"


class TestDegreesNeedTheirSection:
    def test_a_degree_under_the_heading_is_read(self):
        structure = extract_structure(
            _cv("Education", "Master of Science", "Computer Engineering", "2014")
        )

        assert len(structure.formations) == 1
        assert structure.formations[0].annee == "2014"

    def test_an_abbreviation_is_only_a_degree_inside_the_section(self):
        """`M.S` is a Master under an Education heading and part of "MS Office"
        anywhere else. The heading is what makes the loose token safe, which is
        why this rule lives here and not in the shared criteria lexicon."""
        inside = extract_structure(
            _cv("Education", "M.S", "Computer Science", "Illinois Institute of Technology")
        )
        outside = extract_structure(
            _cv("Experience", "Analyst", "2019 - 2022", "Reporting with MS Office and MS SQL.")
        )

        assert len(inside.formations) == 1
        assert outside.formations == []

    def test_a_year_outside_an_education_section_is_not_a_diploma(self):
        """Nothing distinguishes a graduation year from a project date except
        the section it sits in. Guessing writes a false diploma date into a
        submitted CV."""
        structure = extract_structure(
            _cv("Summary", "Delivered a migration in 2018 and another in 2021.")
        )

        assert structure.formations == []

    def test_the_most_recent_degree_comes_first(self):
        structure = extract_structure(
            _cv(
                "Education",
                "Bachelor of Science",
                "Accounting",
                "2010",
                "Master of Science",
                "Finance",
                "2013",
            )
        )

        assert structure.formations[0].annee == "2013"


class TestItRefusesRatherThanGuesses:
    def test_no_text_is_reported_as_such(self):
        assert extract_structure("").status == "no_text"
        assert extract_structure(None).status == "no_text"

    def test_a_cv_with_no_recognisable_section_says_so(self):
        structure = extract_structure(_cv("Passionate professional", "Team player"))

        assert structure.status == "no_sections"
        assert structure.experiences == []

    def test_an_implausible_number_of_roles_yields_none(self):
        """Fifty roles out of one CV means the anchors caught a table of
        figures. Returning the longest plausible prefix would be inventing a
        boundary; returning nothing says the document was not understood."""
        lines = ["Experience"]
        for year in range(1960, 2020):
            lines += [f"Analyst {year}", f"{year} - {year + 1}", "Did the work described."]

        structure = extract_structure(_cv(*lines))

        assert structure.experiences == []

    def test_the_most_recent_role_comes_first(self):
        structure = extract_structure(
            _cv(
                "Experience",
                "Junior Developer",
                "2015 - 2018",
                "Maintained the legacy billing application.",
                "Senior Developer",
                "2019 - 2022",
                "Led the migration to the new platform.",
            )
        )

        assert structure.experiences[0].poste == "Senior Developer"

    def test_the_result_serialises_without_private_fields(self):
        payload = extract_structure(
            _cv("Experience", "Developer", "2019 - 2022", "Built the integration layer.")
        ).to_dict()

        assert set(payload) == {"experiences", "formations", "status", "anonymised_fields"}
        assert "_order" not in payload["experiences"][0]


class TestAnEmployerMustLookLikeAName:
    """Observed on a real generated CV: the first line of a mission block was
    filed as the employer and printed as one in a contractual document."""

    def test_a_mission_sentence_is_not_an_employer(self):
        structure = extract_structure(
            _cv(
                "Experience",
                "IT Analyst Intern",
                "05/2011 - 12/2011",
                "Assisted IT Admin for managing user access rights, user groups "
                "and documentation upload",
            )
        )

        assert structure.experiences[0].poste == "IT Analyst Intern"
        assert structure.experiences[0].employeur is None

    def test_a_short_company_name_survives(self):
        structure = extract_structure(
            _cv(
                "Experience",
                "Inetum Tunisie",
                "2019 - 2022",
                "Software Engineer",
                "Delivered the payments integration for a national bank.",
            )
        )

        assert structure.experiences[0].employeur == "Inetum Tunisie"


class TestMissionsStopAtTheNextSection:
    def test_the_education_section_does_not_leak_into_a_role(self):
        """A CV with no "Experience" heading anchors on the whole document.
        Without a stop rule the last role absorbed the entire education list —
        seen in a produced document, where degrees appeared inside a job
        description."""
        structure = extract_structure(
            _cv(
                "Software Engineer",
                "2019 - 2022",
                "Delivered the payments integration for a national bank.",
                "Education",
                "Master of Science",
                "Computer Engineering",
                "2018",
            )
        )

        assert "Master of Science" not in structure.experiences[0].missions

    def test_the_mission_box_is_bounded(self):
        long_line = "Delivered a substantial migration programme across teams. " * 40
        structure = extract_structure(
            _cv("Experience", "Consultant", "2019 - 2022", long_line)
        )

        assert len(structure.experiences[0].missions) <= 600


class TestATitleMustLookLikeATitle:
    """A produced expert sheet showed roles called "Identifies and solves" and
    "information while evaluating potential sources of" — fragments of mission
    sentences printed as job titles on a page meant for a buyer.

    Measured over 1 870 entries, dropping the positional fallback entirely
    would have cost 464 titles to delete 29 bad ones. So the fallback stays and
    only badly-shaped candidates are refused."""

    def test_a_lowercase_fragment_is_not_a_title(self):
        structure = extract_structure(
            _cv(
                "Experience",
                "information while evaluating potential sources of",
                "2011 - 2015",
                "Analysed the reporting chain for the finance department.",
            )
        )

        assert structure.experiences[0].poste is None
        # The entry survives: the dates and the missions still carry it.
        assert structure.experiences[0].debut == "2011"
        assert structure.experiences[0].missions

    def test_a_line_ending_on_a_conjunction_is_not_a_title(self):
        structure = extract_structure(
            _cv(
                "Experience",
                "troubleshooting, customer assistance, and/or",
                "2011 - 2015",
                "Handled the service desk for a regional network.",
            )
        )

        assert structure.experiences[0].poste is None

    def test_a_real_title_the_lexicon_does_not_know_is_kept(self):
        """The shape check must not undo the fallback it guards: "Vice
        President of Engineering" matches no role noun and is still a title."""
        structure = extract_structure(
            _cv(
                "Experience",
                "Vice President of Engineering",
                "2011 - 2015",
                "Directed the platform organisation across three teams.",
            )
        )

        assert structure.experiences[0].poste == "Vice President of Engineering"

    def test_a_lexicon_match_is_never_second_guessed(self):
        """A lowercase French title passes through the lexicon branch, which
        the shape check never sees."""
        structure = extract_structure(
            _cv(
                "Experience",
                "chef de projet",
                "2011 - 2015",
                "Pilotage du chantier de migration applicative.",
            )
        )

        assert structure.experiences[0].poste == "chef de projet"

    def test_a_refused_title_does_not_become_the_employer(self):
        """Tightening only the title slot moved the fragment rather than
        removing it: "information while evaluating potential sources of"
        stopped being printed as a job title and started being printed as a
        company. Both fields land on a page a buyer reads."""
        structure = extract_structure(
            _cv(
                "Experience",
                "information while evaluating potential sources of",
                "2011 - 2015",
                "Analysed the reporting chain for the finance department.",
            )
        )

        role = structure.experiences[0]
        assert role.poste is None
        assert role.employeur is None
