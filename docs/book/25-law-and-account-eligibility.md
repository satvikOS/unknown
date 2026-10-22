# Laws, broker rules and jurisdictional eligibility {#law}

This chapter is a dated applicability map for implementation, not a determination of the owner's legal or tax status. The owner is currently in the U.S. and may relocate to India. Physical location, tax residence, immigration status, exchange-control residence, broker legal entity and instrument permissions are different facts. They must be recorded and verified before live operation and reviewed when circumstances change.

## Applicability record

For each account and market, store the relevant legal entity, residence classifications supplied by the owner, account type, enabled products, broker confirmations, applicable rule sources, effective dates and review dates. Mark unresolved questions as blockers. Personal use removes the present customer-facing scope; it does not remove market-abuse, broker, tax, data or exchange-control obligations.

## United States

FINRA Regulatory Notice 26-10 states that new intraday margin standards became effective June 4, 2026, with a phase-in ending October 20, 2027. They replace the prior pattern-day-trader framework, but firms may transition during the permitted period. Therefore the application must verify the broker's actual current treatment rather than hard-code either the former $25,000 rule or an assumption that every broker has already removed it. [S11]

Cash-account funding and settlement rules are separate from margin-account day-trading rules. The initial design requires broker-confirmed eligible settled cash and avoids borrowing. Review the broker's treatment of good-faith/freeriding restrictions and account limitations. [S13] Covered U.S. securities commonly settle on T+1, but product exceptions and actual availability still matter. [S01]

SEC Rule 15c3-5 imposes market-access controls on relevant broker-dealers; its applicability is not automatically transferred wholesale to a personal retail software project. Its pretrade-control principles are useful design benchmarks. [S12] Short selling introduces Regulation SHO and broker requirements, including locate and other restrictions where applicable; initial shorting is disabled. [S42]

Manipulative activity, spoofing, deceptive orders, wash trading and misuse of material nonpublic information are not acceptable strategy objectives. The system should prevent self-crossing where controllable, retain order intent and detect suspicious cancel/order patterns. High cancellation rates alone do not establish intent, but they require operational review.

## India

SEBI's February 4, 2025 retail algorithmic-trading circular and subsequent exchange implementation govern relevant broker API access. [S15] NSE's May 2025 standards describe controls including static-IP arrangements, tagging/registration distinctions and session requirements; later updates exist, including a 2026 circular listed on NSE's current overview. [S16, S17] Treat the current broker implementation and latest applicable circulars as the deployment authority, not an old copied threshold.

Confirm whether the owner is eligible as a resident or nonresident for the selected account and instruments. Obtain broker confirmation of API use, algorithm registration/tagging, rate limits, authentication and permitted hosting arrangement. A personal algorithm is not exempt merely because it has one user.

RBI's forex FAQ explains restrictions on overseas margin remittances and permitted foreign-exchange activity. [S18] An existing overseas account or AWS host does not establish permission to trade every leveraged overseas product after a residence change. Obtain specific advice on FEMA/LRS applicability and permitted funding before enabling affected instruments. The system must not offer routing intended to bypass these restrictions.

## Europe

MiFID II's own-account exemptions have conditions and exceptions; high-frequency algorithmic techniques and other activities can alter the analysis. [S19] Determine the actual account entity, venue access arrangement and activity before concluding whether registration or organizational requirements apply. Retail investing through a broker and operating a direct-access trading firm are not the same regulatory posture.

The Market Abuse Regulation addresses prohibited conduct across relevant instruments and venues. [S20] Data licensing, privacy, product restrictions, transaction taxes and reporting responsibilities vary by instrument and jurisdiction. The broker may perform some reporting, but that does not eliminate the owner's recordkeeping obligations.

## Tax and records

Maintain complete trades, lots, fees, currency conversions, corporate actions and external flows across sessions and brokers. A session reset is not a tax reset. U.S. trader tax status and elections are fact-dependent; frequent automated trading alone does not settle them. [S21] Indian and other tax treatment requires review of residence, source, instrument and applicable treaties. The book intentionally does not invent a tax rate for an unknown status.

Distinguish tax wash-sale rules from manipulative wash trading. Keep records sufficient for a qualified tax preparer and broker reconciliation. Tax estimates may be reserved as policy funds, but automated filing and tax elections are outside the initial product.

## Live gate

Before live activation, resolve account entity/type, permitted instruments, residence implications, source entitlements, authorized capital, broker API conditions and tax-record requirements. Recheck after relocation, broker migration or regulatory change. Research and paper simulation can proceed while these facts remain unresolved; live orders cannot.

