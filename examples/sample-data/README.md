# SEC sample data

`sec-facts-sample.jsonl` holds 834 filing-level annual facts for eight issuers
(IBM, Abbott Laboratories, Pfizer, Citigroup, Walmart, Apple, Nike and Exxon
Mobil) and four concepts: `us-gaap:Revenues`,
`us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax`,
`us-gaap:SalesRevenueNet` and `us-gaap:NetIncomeLoss`, unit USD.

The rows come from SEC EDGAR companyfacts (public data) downloaded on
23 October 2026 and parsed by `pit_memory.sec.parse_companyfacts`. Each line is
one `Fact`: the value, fiscal period, form, accession number, filing date, the
first XNYS session after the filing date (`available_on`), a link to the filing
index on sec.gov and the SHA-256 digest of the downloaded companyfacts file.
The issuers were chosen because their filings contain visible restatements,
for example the recast of IBM's 2019 revenue after a spin-off and Abbott's
2011 and 2012 revenue after a separation.

Run the full analysis offline on this sample:

```bash
pit-memory study --facts examples/sample-data/sec-facts-sample.jsonl \
  --universe examples/universe.txt --output runs/sample-study
```
