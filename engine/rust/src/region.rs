//! Regional metadata comes from the host timezone and regional locale, never launcher settings.
#[cfg(not(test))]
use crate::Error;
use crate::{require, Result};
use serde_json::{json, Value};
#[derive(Clone)]
pub struct Region {
    pub country: String,
    pub language: String,
}
pub const COUNTRIES: &str = "AD AE AF AG AI AL AM AO AQ AR AS AT AU AW AX AZ BA BB BD BE BF BG BH BI BJ BL BM BN BO BQ BR BS BT BV BW BY BZ CA CC CD CF CG CH CI CK CL CM CN CO CR CU CV CW CX CY CZ DE DJ DK DM DO DZ EC EE EG EH ER ES ET FI FJ FK FM FO FR GA GB GD GE GF GG GH GI GL GM GN GP GQ GR GS GT GU GW GY HK HM HN HR HT HU ID IE IL IM IN IO IQ IR IS IT JE JM JO JP KE KG KH KI KM KN KP KR KW KY KZ LA LB LC LI LK LR LS LT LU LV LY MA MC MD ME MF MG MH MK ML MM MN MO MP MQ MR MS MT MU MV MW MX MY MZ NA NC NE NF NG NI NL NO NP NR NU NZ OM PA PE PF PG PH PK PL PM PN PR PS PT PW PY QA RE RO RS RU RW SA SB SC SD SE SG SH SI SJ SK SL SM SN SO SR SS ST SV SX SY SZ TC TD TF TG TH TJ TK TL TM TN TO TR TT TV TW TZ UA UG UM US UY UZ VA VC VE VG VI VN VU WF WS YE YT ZA ZM ZW";
impl Region {
    pub fn load() -> Self {
        let locale = sys_locale::get_locale().unwrap_or_else(|| "en".into());
        Self {
            country: country(iana_time_zone::get_timezone().ok().as_deref(), &locale),
            language: language(&locale),
        }
    }

    pub fn gds(&self) -> Result<Value> {
        require(
            COUNTRIES.split_whitespace().any(|c| c == self.country),
            "No se pudo determinar el país del sistema. Revisa la configuración regional o la zona horaria del sistema.",
        )?;
        // Offline transport tests use a fixed clock context; Wine does not implement
        // Windows.Globalization.Calendar used by the native timezone provider.
        #[cfg(test)]
        let (timezone, offset) = ("Etc/UTC".to_owned(), 0);
        #[cfg(not(test))]
        let (timezone, offset) = (
            iana_time_zone::get_timezone()
                .map_err(|_| Error::new("No se pudo determinar la zona horaria del sistema."))?,
            chrono::Local::now().offset().local_minus_utc() / 60,
        );
        Ok(
            json!({"is_default":false,"nation":self.country,"regulation":"ETC","timezone":timezone,"utc_offset":offset,"lang":self.language.split('-').next().unwrap_or("en")}),
        )
    }
}
pub fn language(value: &str) -> String {
    let value = value
        .split('.')
        .next()
        .unwrap_or("")
        .split('@')
        .next()
        .unwrap_or("")
        .replace('_', "-");
    if value.len() <= 64
        && value.split('-').all(|part| {
            !part.is_empty() && part.len() <= 8 && part.bytes().all(|c| c.is_ascii_alphanumeric())
        })
        && value
            .split('-')
            .next()
            .is_some_and(|p| p.len() >= 2 && p.bytes().all(|c| c.is_ascii_alphabetic()))
    {
        value
    } else {
        "en".into()
    }
}

/// A city timezone is more specific than language-region (e.g. en-US on a DO computer).
/// UTC offsets alone are shared by many countries and cannot identify a country.
pub fn country(timezone: Option<&str>, locale: &str) -> String {
    if let Some(zone) = timezone {
        if let Some(country) = include_str!("../../runtime/zone-country.tsv")
            .lines()
            .filter(|line| !line.starts_with('#'))
            .filter_map(|line| line.split_once('\t'))
            .find_map(|(country, name)| (name == zone).then_some(country))
        {
            return country.into();
        }
    }
    language(locale)
        .split('-')
        .skip(1)
        .map(str::to_ascii_uppercase)
        .find(|part| COUNTRIES.split_whitespace().any(|code| code == part))
        .unwrap_or_default()
}
