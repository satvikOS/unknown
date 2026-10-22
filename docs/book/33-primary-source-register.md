# Primary-source register {#sources}

Research snapshot: October 22, 2026. These sources support dated external facts and public methods. Original architecture, numerical examples and policy choices are this project's proposed design. Recheck mutable API, price, broker and legal sources before implementation or deployment. A source citation is not a determination that every provision applies to this owner.

## Market mechanics, data and brokers

**S01. SEC Investor.gov, T+1 settlement investor bulletin.** [Primary source](https://www.investor.gov/introduction-investing/general-resources/news-alerts/alerts-bulletins/investor-bulletins/new-t1-settlement-cycle-what-investors-need-know-investor-bulletin). U.S. covered-security settlement transition; product exceptions still matter.

**S02. Python, Decimal documentation.** [Primary source](https://docs.python.org/3/library/decimal.html). Exact decimal arithmetic facilities and context behavior; production precision remains an application decision.

**S03. Federal Reserve Bank of St. Louis, ALFRED.** [Primary source](https://alfred.stlouisfed.org/). Historical vintages for revised economic series.

**S04. Interactive Brokers, TWS API introduction.** [Primary source](https://www.interactivebrokers.com/docs/tws-api/doc/introduction). Official socket API architecture and supported integration path.

**S05. Interactive Brokers, TWS pacing limitations.** [Primary source](https://www.interactivebrokers.com/docs/tws-api/doc/pacing-limitations/introduction). Request allowance depends on market-data-line entitlement; verify account-specific behavior.

**S06. Interactive Brokers, daily and weekly reauthentication.** [Primary source](https://www.interactivebrokers.com/docs/tws-api/doc/tws-settings/daily-weekly-reauthentication). Authentication/restart constraints relevant to unattended operation.

**S07. Interactive Brokers, paper versus live trading.** [Primary source](https://www.interactivebrokers.com/campus/trading-lessons/paper-trading-vs-live-trading-whats-the-difference/). Paper execution differs from real venue execution.

**S08. Zerodha, Kite Connect v3 documentation.** [Primary source](https://kite.trade/docs/connect/v3/). Candidate India adapter API reference; account eligibility is separate.

**S09. Zerodha, Kite user authentication.** [Primary source](https://kite.trade/docs/connect/v3/user/). Session/token contract; current broker rules require verification.

**S10. Alpaca, order documentation.** [Primary source](https://docs.alpaca.markets/docs/trading/orders/). Candidate additional broker's order semantics, not an eligibility recommendation.

## Jurisdictions and account constraints

**S11. FINRA, Regulatory Notice 26-10.** [Primary source](https://www.finra.org/rules-guidance/notices/26-10). Published April 20, 2026; intraday-margin transition effective June 4, 2026, phase-in through October 20, 2027. Verify the actual broker's transition state.

**S12. SEC, Rule 15c3-5 market-access compliance guide.** [Primary source](https://www.sec.gov/files/rules/final/2010/34-63241-secg.htm). Broker-dealer obligations and useful control principles; not automatic personal-retail applicability.

**S13. SEC Investor.gov, cash-account trading bulletin.** [Primary source](https://www.investor.gov/introduction-investing/general-resources/news-alerts/alerts-bulletins/investor-bulletins/updated-9). Cash funding restrictions; interpret older examples with current settlement rules.

**S14. SEC Investor.gov, trading-order bulletin.** [Primary source](https://www.investor.gov/introduction-investing/general-resources/news-alerts/alerts-bulletins/investor-bulletins-15). Order-type behavior and stop-order limitations.

**S15. SEBI, safer retail participation in algorithmic trading, February 4, 2025.** [Official circular PDF](https://www.sebi.gov.in/sebi_data/attachdocs/feb-2025/1738665456458.pdf). Read with subsequent implementation and extension notices.

**S16. NSE, implementation standards, May 5, 2025.** [Official circular PDF](https://nsearchives.nseindia.com/content/circulars/INVG67858.pdf). Baseline API/algorithm controls; later updates may modify requirements.

**S17. NSE, algorithmic-trading overview and circular index.** [Primary source](https://www.nseindia.com/static/trade/platform-services-non-neat-decision-support-tools-algorithm-trading). Current overview lists later material, including April 30, 2026 circular NSE/INVG/73992. That later PDF's full text was not retrieved in this research pass; broker/latest-circular confirmation remains a live gate.

**S18. Reserve Bank of India, Forex Transactions FAQ.** [Primary source](https://rbi.org.in/scripts/FAQView.aspx?Id=146). Permitted activity and overseas-margin/remittance constraints; residency and transaction-specific review required.

**S19. ESMA, MiFID II Article 2 exemptions.** [Primary source](https://www.esma.europa.eu/publications-and-data/interactive-single-rulebook/mifid-ii/article-2-exemptions). Own-account exemptions have conditions and exceptions.

**S20. ESMA, Market Integrity.** [Primary source](https://www.esma.europa.eu/esmas-activities/markets-and-infrastructure/market-integrity). Market-abuse framework and prohibited-conduct categories. [Regulation 596/2014 entry](https://eur-lex.europa.eu/eli/reg/2014/596/oj/eng); consolidated text retrieval encountered a site verification wall, so detailed current applicability must be checked separately.

**S21. IRS, Topic 429: traders in securities.** [Primary source](https://www.irs.gov/taxtopics/tc429). Fact-dependent U.S. trader tax status and related treatment; no personal tax determination is made here.

## Public quantitative research

**S22. Marco Avellaneda and Sasha Stoikov, High-frequency trading in a limit order book.** [Author-hosted paper](https://people.orie.cornell.edu/sfs33/LimitOrderBook.pdf). Public stylized inventory/quote model; assumptions differ from a production exchange system.

**S23. Robert Almgren and Neil Chriss, Optimal execution of portfolio transactions.** [Original publisher entry](https://www.risk.net/journal-risk/2161150/optimal-execution-portfolio-transactions). Public impact-versus-risk framework; the book presents a simplified educational quadratic objective.

**S24. David H. Bailey and Marcos Lopez de Prado, The Deflated Sharpe Ratio.** [Author-hosted paper](https://www.davidhbailey.com/dhbpapers/deflated-sharpe.pdf). Selection/multiple-testing and distributional issues in reported performance. [Related author-hosted backtest-overfitting paper](https://www.davidhbailey.com/dhbpapers/backtest-prob.pdf). These methods require their own assumptions; they are additional diagnostics.

## Models and development

**S25. OpenAI API pricing.** [Primary source](https://developers.openai.com/api/docs/pricing). Dated model-price examples in Chapter 19; service tier, context, caching and region affect actual charges.

**S26. xAI model catalog and pricing.** [Primary source](https://docs.x.ai/developers/models). Dated Grok example; verify current IDs, capabilities and tariffs before paid use.

**S27. OpenAI structured outputs.** [Primary source](https://developers.openai.com/api/docs/guides/structured-outputs). Schema-constrained output capabilities; not a truth or authorization guarantee.

**S28. xAI structured outputs.** [Primary source](https://docs.x.ai/developers/model-capabilities/text/structured-outputs). Provider-specific structured-output contract.

**S29. Anthropic, Claude Code memory.** [Primary source](https://code.claude.com/docs/en/memory). Project instructions and memory behavior; use actual permission boundaries for enforcement.

## Engineering standards and infrastructure

**S30. NIST SP 800-218, Secure Software Development Framework, final v1.1.** [Primary source](https://csrc.nist.gov/pubs/sp/800/218/final). Secure-development reference; distinguish later drafts from finalized revisions.

**S31. NIST AI Risk Management Framework.** [Primary source](https://www.nist.gov/itl/ai-risk-management-framework). Governance and evaluation reference, not a certification claimed by this project.

**S32. OWASP Application Security Verification Standard.** [Primary source](https://asvs.owasp.org/). Application-security verification structure; map controls to evidence.

**S33. AWS EC2, IMDSv2 configuration.** [Primary source](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/configuring-instance-metadata-service.html). Instance metadata access controls.

**S34. AWS Secrets Manager best practices.** [Primary source](https://docs.aws.amazon.com/secretsmanager/latest/userguide/best-practices.html). Secret access, storage and rotation guidance.

**S35. SQLite write-ahead logging.** [Primary source](https://www.sqlite.org/wal.html). Single-host/concurrency/checkpoint constraints and current WAL-reset advisory. The offline reference uses rollback journaling; evaluate production version and storage separately.

**S36. SEC EDGAR application programming interfaces.** [Primary source](https://www.sec.gov/search-filings/edgar-application-programming-interfaces). Official filings/company-data access; obey current access conditions.

## Public institutional descriptions and rights

**S37. Hudson River Trading, machine learning.** [Primary source](https://www.hudsonrivertrading.com/machine-learning/). Public description only; no claim to private strategy replication.

**S38. Jane Street, machine learning.** [Primary source](https://www.janestreet.com/join-jane-street/machine-learning/). Public research/engineering context only.

**S39. Citadel Securities, quantitative research.** [Primary source](https://www.citadelsecurities.com/careers/quantitative-research/). Public role description; not disclosure of internal algorithms.

**S40. Open Source Initiative, license catalog.** [Primary source](https://opensource.org/licenses). License permissions differ from ownership; review each actual dependency license.

**S41. SEC Investor.gov, margin accounts.** [Primary source](https://www.investor.gov/introduction-investing/general-resources/news-alerts/alerts-bulletins/investor-bulletins-29). Additional background on borrowing and loss pathways.

**S42. SEC, Regulation SHO overview.** [Primary source](https://www.sec.gov/investor/pubs/regsho.htm). Short-sale regulatory background; shorting is disabled in the initial policy.

**S43. Zerodha, Kite errors and limits.** [Primary source](https://kite.trade/docs/connect/v3/exceptions/). Adapter implementation reference; use current account/broker limits rather than copying a stale constant.

## Maintenance rule

When a source changes, record the new retrieval date, affected claims, configuration changes and tests. Preserve the previous research manifest where permitted. A model's recollection cannot supersede the current official broker or regulatory contract. Resolve source conflicts before enabling the affected capability.
