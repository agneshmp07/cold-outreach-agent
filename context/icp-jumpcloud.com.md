# ICP - JumpCloud

> Drafted by profile.py from the company's own website. Read it and cut what is wrong before trusting it. A generated ICP that nobody graded is a guess with formatting.

## Who this company sells to

JumpCloud sells to small to mid-market companies, typically ranging from 20 to 1,000 employees, in cloud-centric industries like technology, digital services, and modern media. Good prospects have remote or hybrid workforces using a mix of operating systems (macOS, Windows, Linux) and cloud applications like Google Workspace. Bad prospects are legacy, on-premise, single-OS environments that do not manage remote access or multiple platforms.


**This describes who PAYS:** IT Directors, VPs of Infrastructure/IT, and Chief Information Security Officers (CISOs).

_Business model: direct. Day-to-day users: IT administrators, system specialists, and security engineers who manage user access and company devices.._

## Who fits

| Industry | Company size | What must be true | Why they would care |
| --- | --- | --- | --- |
| Software & Technology | 50 to 500 employees | Uses a hybrid OS fleet (Macs, Linux, Windows) and cloud email/workspace suites. | Consolidates identity, single sign-on, and device management into one tool, eliminating the need to maintain on-premise Active Directory. |
| Digital Media & Professional Services | 20 to 250 employees | Employs a distributed or remote workforce requiring standardized device onboarding. | Secures employee devices remotely and simplifies IT onboarding without requiring complex network infrastructure. |

## Who does NOT fit

| Looks similar but is wrong | Why |
| --- | --- |
| Enterprise organizations running 100% on-premise Windows domains | Deeply entrenched in traditional Active Directory and legacy network architecture, making cloud directory migration prohibitively complex. |
| Micro-businesses under 10 employees without dedicated IT staff | Sufficiently served by basic Google Workspace or Microsoft 365 native admin tools without needing separate identity and device orchestration. |
| Brick-and-mortar retail businesses with static point-of-sale hardware | Minimal cloud software complexity and few remote identity/device security challenges. |

## Buying signals

| Signal | Where I would see it from outside |
| --- | --- |
| Active job listings hiring an IT Manager, IT Specialist, or Systems Administrator. | LinkedIn Jobs, Indeed, or company careers page. |
| Public postings explicitly stating a remote-first or multi-OS environment requirement (macOS and Windows). | Company career site job descriptions. |
| Company listing Google Workspace alongside third-party SaaS management tools in tech stack requirements. | IT job descriptions on public job boards. |
| Recent funding announcement or rapid headcount growth indicating scaling hiring operations. | Crunchbase, PR Newswire, or LinkedIn Company Insights. |

## Weakest assumption

The assumption that prospect IT buyers care specifically about governance of 'AI agent identities' in addition to standard human employee access. A human can check this by reviewing whether target job listings or tech blogs from prospects actively discuss deploying autonomous AI agents.

## How to grade this

Before you use it, answer these four. They take five minutes and they are the difference between an ICP and a wish list.

1. Could you actually SEE every buying signal from outside? Cross out the ones you could not.
2. Is the does-not-fit table genuinely different from the inverse of the fits table? If not, the description of your product was too vague - rewrite it and re-run.
3. Does this ICP rule out any real company you were considering? If it rules out nobody, it is useless.
4. Do you agree with the weakest assumption it named? If not, name the one you think is actually weakest and write it in.

## Rule

No company name from this file goes into any prompt file. Prompts read this; they do not contain it.

## Also fits — sectors found by sourcing

> Worked out by find_targets.py from what this company sells. These count as fits: a company in one of these sectors is in the ICP even if it does not match a row in the table above.

| Sector | Why they buy | Best fit inside it |
| --- | --- | --- |
| B2B SaaS and Software Development Companies | They employ distributed engineering teams using mixed macOS and Linux devices alongside cloud workspaces, requiring unified identity management and endpoint control without legacy on-premise Active Directory. | A Series B software company with 150 remote employees running on MacBooks and Linux workstations that needs SOC 2 compliance and streamlined onboarding. |
| Digital Marketing, Design, and Media Agencies | Their creative and client-facing staff predominantly use macOS devices across remote locations, making centralized device management and rapid access revocation essential as project teams scale. | A 75-person hybrid digital agency operating entirely on Google Workspace and Mac hardware, needing to secure client data across distributed employee laptops. |
| Fintech and Digital Health Platforms | They face strict regulatory compliance mandates that require enforced device encryption, strict access policies, and audit logs across non-standard, modern device fleets. | A 120-employee telehealth startup scaling rapidly that needs to enforce password policies, multi-factor authentication, and disk encryption across all employee Mac and Windows endpoints. |
| Video Game Development and Interactive Entertainment Studios | Their technical staff rely on high-performance Linux and macOS workstations alongside Windows PCs, requiring cross-platform access control and secure remote device management. | An independent game studio with 90 remote developers and artists working across Windows, Mac, and Linux environments. |
