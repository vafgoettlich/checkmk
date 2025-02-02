// Copyright (C) 2023 Checkmk GmbH - License: GNU General Public License v2
// This file is part of Checkmk (https://checkmk.com). It is subject to the terms and
// conditions defined in the file COPYING, which is part of this source code package.

use crate::check::{
    self, CheckResult, Collection, LevelsChecker, LevelsCheckerArgs, OutputType, Real,
    SimpleCheckResult,
};
use std::collections::HashSet;
use std::convert::AsRef;
use std::fmt::{Display, Formatter, Result as FormatResult};
use time::Duration;
use typed_builder::TypedBuilder;
use x509_parser::certificate::{BasicExtension, Validity, X509Certificate};
use x509_parser::error::X509Error;
use x509_parser::extensions::{GeneralName, SubjectAlternativeName};
use x509_parser::prelude::FromDer;
use x509_parser::prelude::{oid2sn, oid_registry, AlgorithmIdentifier};
use x509_parser::public_key::PublicKey;
use x509_parser::signature_algorithm::SignatureAlgorithm as X509SignatureAlgorithm;
use x509_parser::time::ASN1Time;
use x509_parser::x509::{AttributeTypeAndValue, SubjectPublicKeyInfo};

macro_rules! unwrap_into {
    ($($e:expr),* $(,)?) => {
        {
            vec![
            $( $e.unwrap_or_default().into(), )*
            ]
        }
    };
}

macro_rules! check_eq {
    ($name:tt, $left:expr, $right:expr $(,)?) => {
        if &$left == &$right {
            SimpleCheckResult::notice(format!("{}: {}", $name, $left))
        } else {
            SimpleCheckResult::warn(format!("{} is {} but expected {}", $name, $left, $right))
        }
    };
}

fn first_of<'a>(iter: &mut impl Iterator<Item = &'a AttributeTypeAndValue<'a>>) -> &'a str {
    iter.next()
        .and_then(|a| a.as_str().ok())
        .unwrap_or_default()
}

#[allow(non_camel_case_types)]
#[allow(clippy::upper_case_acronyms)]
#[derive(Debug, PartialEq)]
pub enum SignatureAlgorithm {
    RSA,
    RSASSA_PSS(String),
    RSAAES_OAEP(String),
    DSA,
    ECDSA,
    ED25519,
}

impl Display for SignatureAlgorithm {
    fn fmt(&self, f: &mut Formatter) -> FormatResult {
        write!(
            f,
            "{}",
            match self {
                Self::RSA => "RSA".to_string(),
                Self::RSASSA_PSS(hash) => format!("RSASSA_PSS-{}", hash.to_uppercase()),
                Self::RSAAES_OAEP(hash) => format!("RSAAES_OAEP-{}", hash.to_uppercase()),
                Self::DSA => "DSA".to_string(),
                Self::ECDSA => "ECDSA".to_string(),
                Self::ED25519 => "ED25519".to_string(),
            }
        )?;
        Ok(())
    }
}

#[derive(Debug, TypedBuilder)]
#[builder(field_defaults(default))]
pub struct Config {
    pubkey_algorithm: Option<String>,
    pubkey_size: Option<usize>,
    serial: Option<String>,
    signature_algorithm: Option<SignatureAlgorithm>,
    subject_cn: Option<String>,
    subject_alt_names: Option<Vec<String>>,
    subject_o: Option<String>,
    subject_ou: Option<String>,
    issuer_cn: Option<String>,
    issuer_o: Option<String>,
    issuer_ou: Option<String>,
    issuer_st: Option<String>,
    issuer_c: Option<String>,
    not_after: Option<LevelsChecker<Duration>>,
    max_validity: Option<Duration>,
}

pub fn check(der: &[u8], config: Config) -> Collection {
    let cert = match X509Certificate::from_der(der) {
        Ok((_rem, cert)) => cert,
        Err(_) => check::abort("Failed to parse certificate"),
    };

    Collection::from(&mut unwrap_into!(
        config.subject_cn.map(|expected| {
            let subject_cn = first_of(&mut cert.subject().iter_common_name());
            if subject_cn == expected {
                SimpleCheckResult::ok_with_details(
                    format!("CN={}", subject_cn),
                    format!("Subject CN: {}", subject_cn),
                )
            } else {
                SimpleCheckResult::warn(format!(
                    "Subject CN is {} but expected {}",
                    subject_cn, expected
                ))
            }
        }),
        check_subject_alt_names(cert.subject_alternative_name(), config.subject_alt_names),
        config.subject_o.map(|expected| {
            check_eq!(
                "Subject O",
                first_of(&mut cert.subject().iter_organization()),
                expected
            )
        }),
        config.subject_ou.map(|expected| {
            check_eq!(
                "Subject OU",
                first_of(&mut cert.subject().iter_organizational_unit()),
                expected
            )
        }),
        check_serial(cert.raw_serial_as_string(), config.serial),
        config.issuer_cn.map(|expected| {
            check_eq!(
                "Issuer CN",
                first_of(&mut cert.issuer().iter_common_name()),
                expected
            )
        }),
        config.issuer_o.map(|expected| {
            check_eq!(
                "Issuer O",
                first_of(&mut cert.issuer().iter_organization()),
                expected
            )
        }),
        config.issuer_ou.map(|expected| {
            check_eq!(
                "Issuer OU",
                first_of(&mut cert.issuer().iter_organizational_unit()),
                expected
            )
        }),
        config.issuer_st.map(|expected| {
            check_eq!(
                "Issuer ST",
                first_of(&mut cert.issuer().iter_state_or_province()),
                expected
            )
        }),
        config.issuer_c.map(|expected| {
            check_eq!(
                "Issuer C",
                first_of(&mut cert.issuer().iter_country()),
                expected
            )
        }),
        check_signature_algorithm(&cert.signature_algorithm, config.signature_algorithm),
        check_pubkey_algorithm(cert.public_key(), config.pubkey_algorithm),
        check_pubkey_size(cert.public_key(), config.pubkey_size),
        check_validity_not_after(
            cert.validity().time_to_expiration(),
            config.not_after,
            cert.validity().not_after,
        )
        .map(|cr: CheckResult<Duration>| cr.map(|x| Real::from(x.whole_days() as isize))),
        check_max_validity(cert.validity(), config.max_validity),
    ))
}

fn check_serial(serial: String, expected: Option<String>) -> Option<SimpleCheckResult> {
    expected.map(|expected| check_eq!("Serial", serial.to_lowercase(), expected.to_lowercase()))
}

fn check_subject_alt_names(
    alt_names: Result<Option<BasicExtension<&SubjectAlternativeName<'_>>>, X509Error>,
    expected: Option<Vec<String>>,
) -> Option<SimpleCheckResult> {
    expected.map(|expected| match alt_names {
        Err(err) => SimpleCheckResult::crit(format!("Subject alt names: {}", err)),
        Ok(None) => {
            if expected.is_empty() {
                SimpleCheckResult::notice("No subject alt names")
            } else {
                SimpleCheckResult::warn("No subject alt names")
            }
        }
        Ok(Some(ext)) => {
            let found = HashSet::<&str>::from_iter(ext.value.general_names.iter().flat_map(
                |name| match name {
                    GeneralName::DNSName(v) => Some(*v),
                    _ => None,
                },
            ));
            let expected = HashSet::from_iter(expected.iter().map(AsRef::as_ref));
            if found.is_superset(&expected) {
                SimpleCheckResult::notice("Subject alt names present")
            } else {
                SimpleCheckResult::warn(format!(
                    "Subject alt names: missing {}",
                    expected
                        .difference(&found)
                        .map(|s| format!(r#""{s}""#))
                        .collect::<Vec<_>>()
                        .join(", ")
                ))
            }
        }
    })
}

fn check_signature_algorithm(
    signature_algorithm: &AlgorithmIdentifier,
    expected: Option<SignatureAlgorithm>,
) -> Option<SimpleCheckResult> {
    expected.map(|expected| {
        fn format_oid(oid: &oid_registry::Oid) -> String {
            match oid2sn(oid, oid_registry()) {
                Ok(s) => s.to_owned(),
                _ => format!("{}", oid),
            }
        }

        check_eq!(
            "Signature algorithm",
            match X509SignatureAlgorithm::try_from(signature_algorithm) {
                Ok(X509SignatureAlgorithm::RSA) => "RSA".to_string(),
                Ok(X509SignatureAlgorithm::RSASSA_PSS(params)) => format!(
                    "RSASSA_PSS-{}",
                    format_oid(params.hash_algorithm_oid()).to_uppercase()
                ),
                Ok(X509SignatureAlgorithm::RSAAES_OAEP(params)) => format!(
                    "RSAAES_OAEP-{}",
                    format_oid(params.hash_algorithm_oid()).to_uppercase()
                ),
                Ok(X509SignatureAlgorithm::DSA) => "DSA".to_string(),
                Ok(X509SignatureAlgorithm::ECDSA) => "ECDSA".to_string(),
                Ok(X509SignatureAlgorithm::ED25519) => "ED25519".to_string(),
                Err(_) => return SimpleCheckResult::warn("Signature algorithm: Parser failed"),
            },
            expected.to_string()
        )
    })
}

fn check_pubkey_algorithm(
    pubkey: &SubjectPublicKeyInfo,
    expected: Option<String>,
) -> Option<SimpleCheckResult> {
    expected.map(|expected| {
        check_eq!(
            "Public key algorithm",
            match pubkey.parsed() {
                Ok(PublicKey::RSA(_)) => "RSA",
                Ok(PublicKey::EC(_)) => "EC",
                Ok(PublicKey::DSA(_)) => "DSA",
                Ok(PublicKey::GostR3410(_)) => "GostR3410",
                Ok(PublicKey::GostR3410_2012(_)) => "GostR3410_2012",
                Ok(PublicKey::Unknown(_)) => "Unknown",
                Err(_) => return SimpleCheckResult::warn("Invalid public key"),
            },
            expected
        )
    })
}

fn check_pubkey_size(
    pubkey: &SubjectPublicKeyInfo,
    expected: Option<usize>,
) -> Option<SimpleCheckResult> {
    expected.map(|expected| {
        check_eq!(
            "Public key size",
            match pubkey.parsed() {
                // more or less stolen from upstream `examples/print-cert.rs`.
                Ok(PublicKey::RSA(rsa)) => rsa.key_size(),
                Ok(PublicKey::EC(ec)) => ec.key_size(),
                Ok(PublicKey::DSA(k))
                | Ok(PublicKey::GostR3410(k))
                | Ok(PublicKey::GostR3410_2012(k))
                | Ok(PublicKey::Unknown(k)) => 8 * k.len(),
                Err(_) => return SimpleCheckResult::warn("Invalid public key"),
            },
            expected
        )
    })
}

fn check_validity_not_after(
    time_to_expiration: Option<Duration>,
    levels: Option<LevelsChecker<Duration>>,
    not_after: ASN1Time,
) -> Option<CheckResult<Duration>> {
    levels.map(|levels| match time_to_expiration {
        None => SimpleCheckResult::crit(format!("Certificate expired ({})", not_after)).into(),
        Some(time_to_expiration) => levels.check(
            time_to_expiration,
            OutputType::Notice(format!(
                "Certificate expires in {} day(s) ({})",
                time_to_expiration.whole_days(),
                not_after
            )),
            LevelsCheckerArgs::builder().label("validity").build(),
        ),
    })
}

fn check_max_validity(
    validity: &Validity,
    max_validity: Option<Duration>,
) -> Option<SimpleCheckResult> {
    max_validity.map(|max_validity| {
        if let Some(total_validity) = validity.not_after - validity.not_before {
            match total_validity <= max_validity {
                true => SimpleCheckResult::notice(format!(
                    "Max validity {} days",
                    total_validity.whole_days()
                )),
                false => SimpleCheckResult::warn(format!(
                    "Max validity is {} days but expected at most {}",
                    total_validity.whole_days(),
                    max_validity.whole_days(),
                )),
            }
        } else {
            SimpleCheckResult::crit("Invalid certificate validity")
        }
    })
}

#[cfg(test)]
mod test_check_serial {
    use super::{check_serial, SimpleCheckResult};

    fn s(s: &str) -> String {
        String::from(s)
    }

    #[test]
    fn test_case_insensitive() {
        let result = Some(SimpleCheckResult::notice("Serial: aa:11:bb:22:cc"));

        assert_eq!(
            check_serial(s("aa:11:bb:22:cc"), Some(s("aa:11:bb:22:cc"))),
            result
        );
        assert_eq!(
            check_serial(s("AA:11:BB:22:CC"), Some(s("aa:11:bb:22:cc"))),
            result
        );
        assert_eq!(
            check_serial(s("aa:11:bb:22:cc"), Some(s("AA:11:BB:22:CC"))),
            result
        );
        assert_eq!(
            check_serial(s("AA:11:bb:22:CC"), Some(s("aa:11:BB:22:cc"))),
            result
        );
    }
}
