# Terms Addendum for srcradar

> English | [中文](TERMS_ADDENDUM_zh.md)

> This addendum is the set of the additional restrictions of the use of the srcradar project on top of Apache-2.0, and it is attached to the file LICENSE as an "additional clause" under the article 4 of Apache-2.0.
> This addendum and the LICENSE together make the whole legal text; citing this addendum alone and ignoring the main text of the LICENSE is outside of the protection of the §4 of Apache-2.0.

§A. The permitted uses
------------------------------------------------------------------------

This software is only allowed for the following cases:

1. A security test (penetration testing) that holds an explicit written authorization
   - the owner of the target has signed a written authorization (the scope of the test, the time limit, the whitelist)

2. A bug bounty program (a SRC / a Bug Bounty)
   - in the inventory of the assets of the published SRC platforms (补天, 漏洞盒子, HackerOne and the like)

3. The security operations of a company itself
   - the risk assessment and the continuous monitoring of the assets of the company itself

4. Education and academic research
   - the legal research of the courses of the cybersecurity, of the CTF competitions and of the academic papers

5. The legal collection of the public intelligence (OSINT)
   - only the data that is publicly reachable is collected, and it agrees with the ToS of the target service

------------------------------------------------------------------------
§B. The forbidden uses
------------------------------------------------------------------------

It is strictly forbidden to use this software for:

1. An unauthorized access to a network / to a system / to the data, including without being limited to:
   - the scan, the probe and the intrusion of the ports / of the services / of the web assets of an unauthorized target
   - the breaking of an authentication, the bypass of a mechanism of authorization, the bypass of a WAF / of an anti crawling mechanism
   - the bypass of a captcha, the maintenance of a pool of cookies, the forging of a TLS fingerprint

2. Any activity that breaks the law of the jurisdiction it happens in, including without being limited to:
   - the article 285 of the Criminal Law of the People's Republic of China (the crime of the illegal intrusion into a computer information system)
   - the article 286 of the Criminal Law of the People's Republic of China (the crime of the destruction of a computer information system)
   - the article 253 of the Criminal Law of the People's Republic of China (the crime of the infringement of the personal information of the citizens)
   - the article 32 of the Data Security Law of the People's Republic of China
   - the articles 27 and 44 of the Cybersecurity Law of the People's Republic of China
   - the Computer Fraud and Abuse Act (the United States)
   - the Computer Misuse Act (the United Kingdom)
   - the similar articles of the other jurisdictions

3. The business intelligence / the analysis of the competitors / the crawling of the data (it breaks the ToS of the platform)

4. The mass collection of the personal data (it breaks the Personal Information Protection Law / the GDPR)

5. Providing a tool, a training or a consulting for a criminal activity

------------------------------------------------------------------------
§C. The responsibility of the compliance of the data sources
------------------------------------------------------------------------

The responsibility of the compliance of the data sources of the information of the companies that this software can reach (爱企查, 天眼查, ICP_Query and the like) is entirely borne by the user:

- The user has to assess the ToS, the robots.txt and the access restrictions of every data source on their own

- The configuration of the cookie / of the token of the config.yaml of the upstream ENScan_GO is the responsibility of the user
  - The maintainers of the project are not responsible for the expiry of the cookie of a data source, for a banned account and the like
  - Never commit the cookie / the token that this software obtained into git
    (the .gitignore is already configured to ignore it; whoever breaks that bears the legal consequences)

- The reverse lookup of the ICP filing (the service HG-ha/ICP_Query that the ymicp/ module depends on):
  - That upstream project declares no license (its README only declares that it is for the learning and the exchange)
  - The user assesses the risk of the compliance on their own
  - The maintainers of this project do not endorse the compliance of that upstream

------------------------------------------------------------------------
§D. The disclaimer
------------------------------------------------------------------------

This software is provided "as is", without any warranty of any kind, express or implied.

To the maximum extent that the applicable law allows:

1. The authors and the contributors bear no liability at all for any loss, damage or criminal / civil / administrative liability caused directly or indirectly by the use of this software.

2. The user understands and accepts fully:
   - A tool of the cybersecurity has a dual use by nature, and the author cannot control the behaviour of the user
   - Once this software is used for an unauthorized test, the legal consequences are borne by the user alone

3. This software does not constitute a legal advice, and the user has to consult a professional legal opinion on their own.

4. This disclaimer applies to every jurisdiction and it is fully valid as far as the local law does not forbid it.

5. If any clause is declared invalid by a court that has the jurisdiction, the other clauses are not affected.

6. The open source components that this software contains (ENScan_GO / httpx / dnsx / cdncheck and the like)
   remain bound by their own original licenses, and this addendum neither replaces nor modifies those licenses.

------------------------------------------------------------------------
§E. The termination
------------------------------------------------------------------------

When the user breaks any clause of this addendum:

1. The license under Apache-2.0 terminates automatically
2. The user has to stop using this software immediately
3. The user has to delete every copy that derives from this software immediately
4. The author keeps the right to pursue the legal liability

------------------------------------------------------------------------
§F. The contact and the reports
------------------------------------------------------------------------

When you find a security problem in this software, report it through the GitHub Issues / the Security Advisories,
and do not disclose it publicly.

When you find that this software is used for an activity that breaks the §B, you can report it through the GitHub Issues.

------------------------------------------------------------------------
The date of the entry into force
------------------------------------------------------------------------

This LICENSE comes into force on the day of the first public release of the public version of srcradar.
