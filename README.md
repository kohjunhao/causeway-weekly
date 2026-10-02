# Causeway Weekly

The fortnight ahead on the Johor to Singapore Causeway, every Sunday at 6 pm Singapore time. A jam forecast for Woodlands and Tuas built from the public holiday and school calendars of both countries, every rule change with the date it bites, the ringgit rate, and the six checkpoint cameras captured at build time.

Live: https://causeway-weekly.vercel.app

## How it runs

`build.py` pulls the data and writes the whole site into `public/`. The GitHub Action in `.github/workflows/weekly.yml` runs it every Sunday, commits the output, and opens a GitHub issue whose body is the newsletter. GitHub emails that issue to everyone watching the repo, which is how the owner gets it with zero mail infrastructure.

Run it by hand:

```
pip install requests pillow
python build.py
```

## Facts live in one file

`data/facts.json` holds school holiday ranges, rule changes and ICA record figures, each with a source link. Fix a date there and the next issue is right. Public holidays come from the public Google holiday calendars at build time, so they need no upkeep.

## Email subscribers

The subscribe form posts to `api/subscribe.js`, which stores one private blob per address in Vercel Blob. Until a Blob store is connected to the project the form reports that signup is off and points at the RSS feed. The weekly send (`send.py`) runs only when the repo has `RESEND_API_KEY`, `EXPORT_KEY` and `FROM_EMAIL` secrets; `EXPORT_KEY` must also be set in the Vercel project so the Action can read the list.

## Money

One sponsor slot per issue, plain text, S$200. Natural buyers: JB money changers, clinics and dentists, car insurers (Singapore VEP needs Singapore cover), e-hailing operators, malls and property developers selling to commuters.
