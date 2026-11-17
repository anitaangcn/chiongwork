"""Build sample alert emails shaped like LinkedIn / JobStreet / Indeed job alerts.

These are synthetic: real alert layouts change often, so drop a real exported .eml into
tests/fixtures/ to check the parser against the current format.
"""
from email.message import EmailMessage
from pathlib import Path

OUT = Path(__file__).parent / "fixtures"
OUT.mkdir(exist_ok=True)


def save(name, sender, subject, html):
    m = EmailMessage()
    m["From"], m["To"], m["Subject"] = sender, "me@example.com", subject
    m["Date"] = "Mon, 16 Nov 2026 08:00:00 +0800"
    m.set_content("This email needs HTML.")
    m.add_alternative(html, subtype="html")
    (OUT / name).write_bytes(bytes(m))


save("linkedin.eml", "LinkedIn Job Alerts <jobalerts-noreply@linkedin.com>", "Data Analyst in Singapore: 3 new jobs", """
<table><tr><td>
 <a href="https://www.linkedin.com/comm/jobs/view/4012345678/?trackingId=abc&refId=xyz">Data Analyst</a>
 <p>Stripe · Singapore (Hybrid)</p><p>Actively recruiting</p>
 <a href="https://www.linkedin.com/comm/jobs/view/4012345678/?trackingId=abc">View job</a>
</td></tr><tr><td>
 <a href="https://www.linkedin.com/comm/jobs/view/4098765432/?trackingId=def">Senior Product Designer</a>
 <p>Grab</p><p>Singapore, Singapore</p><p>12 applicants</p>
</td></tr><tr><td>
 <a href="https://www.linkedin.com/comm/jobs/view/4011112222/?trackingId=ghi">Marketing Executive</a>
 <p>Love, Bonito · Singapore</p>
</td></tr></table>
<a href="https://www.linkedin.com/comm/jobs/search?keywords=data">See all jobs</a>
<a href="https://www.linkedin.com/comm/psettings/email-unsubscribe">Unsubscribe</a>
""")

save("jobstreet.eml", "Jobstreet <noreply@s.jobstreet.com>", "5 new jobs for Accountant", """
<div><a href="https://sg.jobstreet.com/job/81234567?type=standard&ref=alert"><b>Accounts Executive</b></a>
<span>ABC Trading Pte Ltd</span><span>Jurong East</span><span>$3,500 – $4,200 per month</span></div>
<div><a href="https://sg.jobstreet.com/job/81234999?ref=alert"><b>Senior Accountant</b></a>
<span>Wise</span><span>Central Region</span></div>
<a href="https://sg.jobstreet.com/job/81234567?ref=alert">View</a>
""")

save("indeed.eml", "Indeed <alert@indeed.com>", "Software Engineer jobs in Singapore", """
<table><tr><td><a href="https://sg.indeed.com/rc/clk/dl?jk=1a2b3c4d5e6f7a8b&from=ja&tk=xyz">Software Engineer, Backend</a></td></tr>
<tr><td>OKX</td></tr><tr><td>Singapore</td></tr>
<tr><td><a href="https://sg.indeed.com/rc/clk/dl?jk=9f8e7d6c5b4a3210&from=ja">Junior Web Developer</a></td></tr>
<tr><td>Tiny Studio Pte Ltd - Bugis</td></tr></table>
""")

save("newsletter.eml", "Some Shop <news@shop.example>", "Sale!", "<a href='https://shop.example/jobs/view/1'>Not a job</a>")
print("wrote", sorted(p.name for p in OUT.iterdir()))
