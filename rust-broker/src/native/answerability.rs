//! Conservative requested-field checks. These veto missing detail, not prove an answer.
use regex::Regex;
use std::sync::OnceLock;

pub fn search_query(query: &str) -> &str {
    static TRACKING: OnceLock<Regex> = OnceLock::new();
    static DEPENDENT: OnceLock<Regex> = OnceLock::new();
    let tracking = TRACKING.get_or_init(|| Regex::new(r"(?i)^\s*(?:request\s+(?:reference|id)|correlation\s+id|trace\s+id|(?:question|query)\s+(?:number|id))\s*[:#]?\s*[0-9]+\s*:\s*((?:how|what|when|where|who|which|why|can|should|do|does|is|are)\b[^\r\n]+)$").unwrap());
    let dependent = DEPENDENT
        .get_or_init(|| Regex::new(r"(?i)\b(?:it|its|this|that|these|those|their)\b").unwrap());
    if let Some(captures) = tracking.captures(query) {
        let question = captures.get(1).unwrap().as_str();
        if !dependent.is_match(question) {
            return question;
        }
    }
    query
}

pub fn supports_requested_field(query: &str, evidence: &str) -> bool {
    let query = search_query(query);
    if !supports_identifiers(query, evidence) || !supports_recovery_method(query, evidence) {
        return false;
    }
    let requested = super::codec::tokenize(query);
    if requested
        .iter()
        .any(|word| word.chars().all(|c| c.is_ascii_digit()))
    {
        let words = super::codec::tokenize(evidence);
        let anchors: Vec<_> = requested
            .iter()
            .filter(|word| {
                word.chars().all(char::is_alphabetic)
                    && ![
                        "how", "who", "when", "why", "which", "my", "our", "should", "can", "will",
                        "do", "be", "by", "it", "as", "on", "at", "have", "has", "about",
                    ]
                    .contains(&word.as_str())
            })
            .collect();
        // A shared number alone cannot establish the requested relationship.
        let shared_number = requested
            .iter()
            .any(|word| word.chars().all(|c| c.is_ascii_digit()) && words.contains(word));
        if shared_number && !anchors.is_empty() && !anchors.iter().any(|word| words.contains(*word))
        {
            return false;
        }
    }
    static SECRET_REQUEST: OnceLock<Regex> = OnceLock::new();
    static SECRET_PROCEDURE: OnceLock<Regex> = OnceLock::new();
    // Credentials are deliberately never evidence in this memory store.
    if SECRET_REQUEST.get_or_init(|| Regex::new(r"(?i)\b(?:what(?:'s| is| are)|show|tell|give|retrieve|recall)\b.{0,60}\b(?:password|passphrase|api key|access token|private key|recovery code|secret key)\b").unwrap()).is_match(query)
        && !SECRET_PROCEDURE.get_or_init(||Regex::new(r"(?i)\b(?:reset|change|rotate|create|recover|protect|manage|secure)\b.{0,30}\b(?:password|passphrase|key|token|code)\b").unwrap()).is_match(query) {
        return false;
    }
    static RULES: OnceLock<Vec<(Regex, Regex)>> = OnceLock::new();
    let rules=RULES.get_or_init(||[
        (r"\b(?:how often|how frequently|what frequency)\b",r"\b(?:hourly|daily|nightly|weekly|fortnightly|monthly|quarterly|annually|yearly|biennially|annual|regular|never|always|continuously|constantly|as needed|as required|when needed|whenever)\b|\b(?:every|each|per|a|an)\s+(?:(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten|other|second)\s+)?(?:seconds?|minutes?|hours?|days?|nights?|weeks?|months?|quarters?|years?|use|uses|cycles?)\b|\b(?:once|twice|\d+ times)\b"),
        (r"\b(?:colour|color)\b",r"\b(?:red|green|blue|yellow|black|white|grey|gray|orange|purple|pink|silver|gold|brown|colour|color)\b"),
        (r"\b(?:serial|sku|imei|isbn)\b",r"\b(?:serial|sku|imei|isbn)\b.{0,40}[a-z0-9][a-z0-9-]{2,}"),
        (r"\b(?:brand|manufactur(?:er|ed|es|ing))\b|\b(?:which|what|who) (?:is the )?maker\b",r"\b(?:brand|manufacturer|manufactured|made by|maker)\b"),
        (r"\b(?:price|cost|fee|salary|wage|budget|rate)\b.{0,35}\b(?:amount|dollars?|pay|paid)\b|\b(?:purchase price|sale price|price tag|salary|hourly rate|annual budget)\b|\bhow much\b.{0,60}\b(?:cost|pay|price|charge)\b",r"(?:[$]|\b(?:AUD|USD|NZD|CAD|GBP|EUR)\s*)\s*\d|\b\d+(?:[.,]\d+)?\s*(?:dollars?|cents?|pounds?|euros?)\b|\b(?:free of charge|no charge|costs? nothing|unpaid)\b"),
        (r"\b(?:warranty|guarantee)\b.{0,40}\b(?:expir\w*|end\w*|date|period|duration)\b|\b(?:when|how long)\b.{0,60}\b(?:warranty|guarantee)\b",r"\b(?:warranty|guarantee|coverage)\s+(?:(?:expires?|ends?)(?: on)?|until|(?:is )?(?:valid )?(?:for|of)|lasts?)\s+(?:\d[\d:./-]*|one|two|three|four|five|six|seven|eight|nine|ten|lifetime|january|february|march|april|may|june|july|august|september|october|november|december)\b|\b(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten)[ -](?:year|month|week|day)s?\s+(?:warranty|guarantee)\b|\b(?:no|without|lifetime)\s+(?:warranty|guarantee)\b"),
        (r"\b(?:name|named)\b|^who (?:is|was) (?:the|our|my)\b",r"\b(?:name(?:d)?|called|appointed|assigned to)\b|\b(?:is|was|by|:|-)\s+[A-Z][a-z]+\s+[A-Z][a-z]+\b"),
    ].into_iter().map(|(q,e)|(Regex::new(&format!("(?i){q}")).unwrap(),Regex::new(e).unwrap())).collect());
    let lower = evidence.to_lowercase();
    let mut requested_fields = 0;
    let mut supported_fields = 0;
    for (request, field) in rules {
        if request.is_match(query) {
            requested_fields += 1;
            if field.is_match(evidence) || field.is_match(&lower) {
                supported_fields += 1;
            }
        }
    }
    // Separate facts may supply separate attributes of an explicit compound request.
    requested_fields == supported_fields || (multiple_topics(query) && supported_fields > 0)
}

fn supports_identifiers(query: &str, evidence: &str) -> bool {
    static TOKENS: OnceLock<Regex> = OnceLock::new();
    let tokens = TOKENS.get_or_init(|| Regex::new(r"(?i)\b[a-z0-9]+(?:[-_][a-z0-9]+)*\b").unwrap());
    let identifiers: Vec<_> = tokens
        .find_iter(query)
        .map(|m| m.as_str())
        .filter(|token| {
            token.len() >= 3
                && token.chars().any(|c| c.is_ascii_alphabetic())
                && token.chars().any(|c| c.is_ascii_digit())
        })
        .collect();
    let available: Vec<_> = tokens.find_iter(evidence).map(|m| m.as_str()).collect();
    let matches = |id: &&str| available.iter().any(|word| word.eq_ignore_ascii_case(id));
    identifiers.is_empty()
        || if multiple_topics(query) {
            identifiers.iter().any(matches)
        } else {
            identifiers.iter().all(matches)
        }
}

fn supports_recovery_method(query: &str, evidence: &str) -> bool {
    static RECOVERY: OnceLock<(Regex, Regex, Regex, Regex)> = OnceLock::new();
    let (asset, hazard, request, method) = RECOVERY.get_or_init(|| (
        Regex::new(r"(?i)\b(?:data|records?|documents?|files?|measurements?|logs?|database|reports?)\b").unwrap(),
        Regex::new(r"(?i)\b(?:losing|loss|lost|corrupt\w*|destroy\w*|destruction|disaster)\b").unwrap(),
        Regex::new(r"(?i)\b(?:how|method|procedure|strategy|safeguard|prevent|protect|avoid|recover|restore)\b").unwrap(),
        Regex::new(r"(?i)\b(?:backups?|back(?:ed)? up|snapshots?|replica\w*|redundan\w*|restor\w*|recover\w*|copies|copied|duplicat\w*|checksu\w*|version(?:ed|ing)?)\b").unwrap(),
    ));
    // A retention period or storage condition is not evidence of recovery capability.
    !(asset.is_match(query) && hazard.is_match(query) && request.is_match(query))
        || method.is_match(evidence)
}

pub fn multiple_topics(query: &str) -> bool {
    static MULTIPLE: OnceLock<Regex> = OnceLock::new();
    MULTIPLE
        .get_or_init(|| {
            Regex::new(r"(?i)\b(?:and|both|compare|comparison|versus|vs|across|all|list)\b")
                .unwrap()
        })
        .is_match(query)
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn frequency_queries_need_timing_evidence_not_incidental_numbers() {
        let query = "How often should we remove scale from the coffee maker?";
        for evidence in [
            "Synthetic load warm_100 32670\nSynthetic fixture value 32670 for concurrent ingestion.",
            "The coffee maker is on the kitchen bench.",
            "The cleaning supplier's invoice is 32670.",
        ] {
            assert!(!supports_requested_field(query, evidence), "{evidence}");
        }
        for evidence in [
            "The espresso appliance is descaled monthly using its approved cleaner.",
            "Descale the appliance every three months.",
            "Clean after each use.",
            "Clean whenever the warning light appears.",
            "Clean twice a week.",
            "Never descale this model with acid.",
        ] {
            assert!(supports_requested_field(query, evidence), "{evidence}");
        }
        assert!(supports_requested_field(
            "Where is the coffee maker?",
            "The coffee maker is on the kitchen bench."
        ));
    }

    #[test]
    fn tracking_projection_preserves_ambiguous_references() {
        let cases: Vec<(String, Option<String>)> =
            serde_json::from_str(include_str!("query_projection_cases.json")).unwrap();
        for (query, expected) in cases {
            assert_eq!(
                search_query(&query),
                expected.as_deref().unwrap_or(&query),
                "{query}"
            );
        }
        assert!(!supports_requested_field(
            "Request reference 144: Who is the finance manager?",
            "The finance manager approves invoices."
        ));
        assert!(!supports_requested_field(
            "Request reference 144: What is the square root of 144?",
            "Inventory reference 144 identifies packing materials."
        ));
    }
    #[test]
    fn topic_is_not_a_missing_attribute() {
        for (query, absent, present) in [
            (
                "What colour is the badge?",
                "Visitors wear temporary badges.",
                "Visitor badges are blue.",
            ),
            (
                "Who is our project manager?",
                "The project manager approves leave.",
                "The project manager is Jordan Vale.",
            ),
            (
                "What is the coordinator's name?",
                "The coordinator records training.",
                "The coordinator is named Casey Wu.",
            ),
            (
                "What is the device serial number?",
                "Devices are entered in an asset register.",
                "The device serial number is AX-442.",
            ),
            (
                "Which manufacturer made the monitor?",
                "The monitor has a three-year warranty.",
                "The manufacturer is Example Devices.",
            ),
        ] {
            assert!(!supports_requested_field(query, absent), "{query}");
            assert!(supports_requested_field(query, present), "{query}");
        }
        assert!(supports_requested_field(
            "Who approves leave?",
            "The project manager approves leave."
        ));
        assert!(!supports_requested_field(
            "What is the password for the corporate VPN?",
            "Use multifactor authentication for remote access."
        ));
        assert!(supports_requested_field(
            "How do I reset my password?",
            "Ask the service desk to reset your password."
        ));
        assert!(supports_requested_field(
            "What is needed to reset a password?",
            "Password resets require service desk identity verification."
        ));
        assert!(!supports_requested_field(
            "What is the square root of 144?",
            "Warehouse shelf 144 holds packaging."
        ));
        assert!(supports_requested_field(
            "What is the square root of 144?",
            "The square root of 144 is 12."
        ));
        assert!(supports_requested_field(
            "Where is pump 144 stored?",
            "Pump 144 is stored in bay 8."
        ));
        assert!(supports_requested_field(
            "Question 144: How should I look after my car?",
            "The automobile needs annual maintenance."
        ));
        assert!(multiple_topics("Compare inspection and repair rules."));
        assert!(!multiple_topics("Who approves release changes?"));
    }

    #[test]
    fn requested_relations_need_the_right_kind_of_evidence() {
        for (query, absent, present) in [
            (
                "Who manufactured the camera?",
                "Jordan is responsible for the camera.",
                "The camera was manufactured by Example Optics.",
            ),
            (
                "How much does camera ZX-24 cost?",
                "Camera ZX-24 needs 12 calibration samples.",
                "Camera ZX-24 costs AUD 240.",
            ),
            (
                "What is the purchase price of the press?",
                "The press is inspected at 09:00.",
                "The purchase price of the press is 400 dollars.",
            ),
            (
                "When does the warranty on the pump expire?",
                "The pump inspection is on Monday at 10:00.",
                "The pump warranty expires on 2028-04-05.",
            ),
            (
                "What is the warranty period?",
                "The pump warranty document is stored in bay 12.",
                "The pump has a three year warranty.",
            ),
            (
                "How do we avoid losing historical measurements?",
                "Measurements are retained for five years.",
                "Measurement snapshots are copied to a second independent server.",
            ),
            (
                "Which method protects files against corruption?",
                "Files are kept in the archive for two years.",
                "Checksums and versioned copies protect files against corruption.",
            ),
            (
                "How can we recover documents after a disaster?",
                "Documents are stored in a cool dry place.",
                "Documents are restored from off-site backups.",
            ),
        ] {
            assert!(
                !supports_requested_field(query, absent),
                "{query}: {absent}"
            );
            assert!(
                supports_requested_field(query, present),
                "{query}: {present}"
            );
        }
        assert!(supports_requested_field(
            "How long must records be retained?",
            "Records are retained for five years."
        ));
        assert!(supports_requested_field(
            "How do I reset my password?",
            "Use the verified recovery email."
        ));
        let compound = "Give the purchase price and warranty expiry date of camera ZX-24.";
        assert!(supports_requested_field(
            compound,
            "Camera ZX-24 costs AUD 240."
        ));
        assert!(supports_requested_field(
            compound,
            "Camera ZX-24 warranty expires on 2028-04-05."
        ));
        assert!(!supports_requested_field(
            compound,
            "Camera ZX-24 is inspected on Monday."
        ));
    }

    #[test]
    fn exact_identifiers_cannot_be_substituted_by_similar_assets() {
        assert!(!supports_requested_field(
            "Where is camera ZX-24?",
            "Camera ZX-25 is in bay 12."
        ));
        assert!(!supports_requested_field(
            "Where is camera ZX24?",
            "Camera ZX240 is in bay 12."
        ));
        assert!(supports_requested_field(
            "Where is camera ZX-24?",
            "Camera zx-24 is in bay 12."
        ));
        assert!(supports_requested_field(
            "Compare ZX-24 and ZX-25.",
            "ZX-25 is inspected on Monday."
        ));
        assert!(!supports_requested_field(
            "Compare ZX-24 and ZX-25.",
            "ZX-26 is inspected on Monday."
        ));
        assert!(supports_requested_field(
            "Where is the camera?",
            "Camera ZX-24 is in bay 12."
        ));
    }
}
