"""
module1_collectors/apollo.py — Apollo.io 联系人采集
免费额度：50条/月
职责：根据租户产品+目标国家，找到对应的进口商决策人
"""
import requests
import time
from typing import Optional


class ApolloCollector:

    JOB_TITLES = [
        "Purchasing Manager", "Import Manager", "Procurement Manager",
        "Sourcing Manager", "Supply Chain Manager", "Buying Manager",
        "General Manager", "CEO", "Owner", "Director of Purchasing",
        "Head of Procurement", "Imports Director",
    ]

    def __init__(self):
        self.api_key        = ""
        self.product_name   = ""
        self.search_keywords = []

    def fetch_all(self, countries: list = None, mock: bool = False) -> list:
        if not countries:
            return []
        if mock or not self.api_key:
            return self._mock_results(countries)

        all_leads = []
        for country in countries[:5]:   # 每次最多5个国家，节省免费额度
            leads = self._search_country(country)
            all_leads.extend(leads)
            time.sleep(1.2)
        return all_leads

    def _search_country(self, country: str) -> list:
        keywords = self.search_keywords[:3] if self.search_keywords else []
        if self.product_name:
            keywords.insert(0, self.product_name)

        try:
            resp = requests.post(
                "https://api.apollo.io/v1/mixed_people/search",
                headers={"Content-Type": "application/json",
                         "x-api-key": self.api_key},
                json={
                    "person_titles":   self.JOB_TITLES[:6],
                    "person_locations": [country],
                    "q_organization_keyword_tags": keywords[:3],
                    "per_page": 10,
                    "page": 1,
                },
                timeout=20,
            )
            resp.raise_for_status()
            data = resp.json()

            leads = []
            for person in data.get("people", []):
                org = person.get("organization") or {}
                name = f"{person.get('first_name','')} {person.get('last_name','')}".strip()
                lead = {
                    "company_name":  org.get("name", ""),
                    "country":       country,
                    "contact_name":  name,
                    "contact_title": person.get("title", ""),
                    "email":         person.get("email", ""),
                    "linkedin_url":  person.get("linkedin_url", ""),
                    "website":       org.get("website_url", ""),
                    "source":        "apollo",
                    "notes":         f"Apollo.io | {person.get('title','')}",
                }
                if lead["company_name"]:
                    leads.append(lead)
            print(f"[Apollo] {country}: {len(leads)} 条联系人")
            return leads

        except Exception as e:
            print(f"[Apollo] {country} 失败: {e}")
            return []

    def _mock_results(self, countries: list) -> list:
        if not countries:
            return []
        return [
            {
                "company_name":  "Lagos Motor Import Co.",
                "country":       countries[0],
                "contact_name":  "James Adebayo",
                "contact_title": "Purchasing Manager",
                "email":         "james@lagosmotor.com",
                "source":        "apollo",
                "notes":         "Apollo.io | Purchasing Manager",
            }
        ]


apollo_collector = ApolloCollector()
