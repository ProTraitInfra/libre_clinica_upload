"""Uploads data to Libre Clinica from SPARQL endpoint."""

import datetime
import logging
import os
import sys

import lxml
import pandas as pd
import requests
import yaml  # type: ignore
from bs4 import BeautifulSoup
from pydantic import BaseModel
from query_engine_libre_clinica_github import QueryEngine
from unidecode import unidecode
from zeep import Client

logger = logging.getLogger(__name__)


def get_lc_ss_oid(
    lc_endpoint: str,
    lc_user: str,
    lc_password: str,
    study_identifier: str,
    ss_label: str,
    ss_gender: str,
    rerun: bool = False,
) -> str:
    """Function responsible for obtaining the SS_OID for a given study subject label
    in Libre Clinica. OIDs are identifiers used to uniquely identify various entities.
    These identifiers are generated and made available to Users on multiple screens.
    OIDs can be found by navigating the user interface (UI) or by downloading the
    study metadata file.

    Args:
        lc_endpoint: endpoint URL of Libre Clinica
        lc_user: username for Libre Clinica
        lc_password: password for Libre Clinica
        study_identifier: identifier of the study to which the study subject belongs.
        ss_label: label of study subject for which we want the SS_OID
        ss_gender: gender of study subject
        rerun: rerun if SS_OID is not found (boolean).

    Returns: the Object Identifier

    """
    logger.info(f"Trying to get SS_OID for {ss_label}")

    # Create SOAP client to interact with LC endpoint.
    client = Client(f"{lc_endpoint}study/v1/studySubjectWsdl.wsdl")
    # It forms the WSDL URL for the study subject service.

    # Construct SOAP envelope header, containing user and password for authentication.
    header = f"""
    <soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/" xmlns:v1="http://openclinica.org/ws/studySubject/v1" xmlns:bean="http://openclinica.org/ws/beans">
        <soapenv:Header>
            <wsse:Security soapenv:mustUnderstand="1" xmlns:wsse="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd">
            <wsse:UsernameToken wsu:Id="UsernameToken-27777511" xmlns:wsu="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd">
                <wsse:Username>{lc_user}</wsse:Username>
                <wsse:Password type="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-username-token-profile-1.0#PasswordText">{lc_password}</wsse:Password>
            </wsse:UsernameToken>
            </wsse:Security>
        </soapenv:Header>
    </soapenv:Envelope>
    """
    header = lxml.etree.fromstring(header)[0][0]

    # Check if subject exists
    subject = {
        "label": ss_label,
        "enrollmentDate": "1900-01-01",  # This is ignored by LC, but we have to supply it
        "subject": {},
        "studyRef": {"identifier": study_identifier},
    }

    # Check if the study subject exists in libre Clinica.
    with client.settings(strict=False):
        ret = client.service.isStudySubject(subject, _soapheaders=[header])

    if ret["result"] == "Success":
        # If yes return the OID from the response:
        oid = ret["_raw_elements"].pop().text
        logger.info(f"Found SS OID {oid}")
        return oid
    elif not rerun:
        # Else create the Study Subject
        logger.info("Couldn't find an OID, creating a new SS")
        subject = {
            "label": ss_label,
            "enrollmentDate": datetime.datetime.now().strftime("%Y-%m-%d"),
            "subject": {"gender": "m" if ss_gender == 1 else "f" if ss_gender == 2 else ss_gender},
            "studyRef": {"identifier": study_identifier},
        }

        with client.settings(strict=False):
            ret = client.service.create(subject, _soapheaders=[header])

        if ret["result"] == "Success":
            # Rerun this method because LC doesn't actually give us back the OID
            logger.info("All went well, rerunning to fetch OID")
            return get_lc_ss_oid(
                lc_endpoint=lc_endpoint,
                lc_user=lc_user,
                lc_password=lc_password,
                study_identifier=study_identifier,
                ss_label=ss_label,
                ss_gender=ss_gender,
                rerun=True,
            )  # rerun the function recursively
        else:
            # Couldn't create user
            logger.warning(f'Could not create user: {ret["error"]}')
            return ""
    else:
        logger.info("A new Study Subject was created but its OID is still not available.")
        return ""


class LCConfig(BaseModel):
    """Base class for Libre Clinica configuration.

    sparql_endpoint: SPARQL endpoint URL
    query: SPARQL query to retrieve the data
    lc_endpoint: Libre Clinica endpoint URL
    lc_user: Libre Clinica username
    lc_password: Hashed password to Libre Clinica
    study_oid: Object identifier of thr study
    study_identifier: study identifier
    event_oid: object identifier of event
    form_oid: object identifier of the form
    item_group_oid: object identifier of the item group
    identifier_colname: column in df containing the identifiers
    gender_colname: column in df containing gender information
    item_prefix: prefiix to add to each item name
    alternative_item_oids: dictionary for mapping column names to item OIDs.
    perform_query: to perform or not the upload configuration query in FAIR station.

    """

    sparql_endpoint: str
    query: str
    lc_endpoint: str
    lc_user: str
    lc_password: str
    study_oid: str
    study_identifier: str
    event_oid: str
    form_oid: str
    item_group_oid: str
    identifier_colname: str
    gender_colname: str
    item_prefix: str
    alternative_item_oids: None
    perform_query: bool = True

    def __init__(self, **kwargs):
        """Handle alternative item OIDs."""
        super().__init__(**kwargs)
        if self.alternative_item_oids is None:
            self.alternative_item_oids = {}


def upload_to_lc(config: LCConfig) -> None:
    """Uploads data to Libre Clinica endpoint from the SPARQL endpoint using SOAP communication.

    Args: Libre Clinica upload configuration object.

    Returns: None, it uploads the data to Libre Clinica.

    """
    if not config.perform_query:
        logger.critical(
            "Libre clinica will not be updated, make sure to set perform_query to True"
            " if you wish to update Libre Clinica\nExiting now"
        )
        sys.exit(0)

    logger.info(f"Retrieving triples from {config.sparql_endpoint}")
    sparql = QueryEngine(config.sparql_endpoint)
    df_sparql = sparql.get_sparql_dataframe(query)

    if len(df_sparql) == 0:
        logger.error(
            "ERROR: There is no patient data available at SPARQL endpoint,"
            " check your FAIR data station for correctness of data\nExiting now"
        )
        sys.exit(1)

    logger.info(f"Received {len(df_sparql)} rows of data")
    df_sparql = df_sparql.rename(columns=config.alternative_item_oids)
    logger.info(f"Have columns {df_sparql.columns}")

    # Create SOAP envelope header.
    header = f"""
    <soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/" xmlns:v1="http://openclinica.org/ws/event/v1" xmlns:bean="http://openclinica.org/ws/beans">
        <soapenv:Header>
            <wsse:Security soapenv:mustUnderstand="1" xmlns:wsse="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd">
            <wsse:UsernameToken wsu:Id="UsernameToken-27777511" xmlns:wsu="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd">
                <wsse:Username>{config.lc_user}</wsse:Username>
                <wsse:Password type="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-username-token-profile-1.0#PasswordText">{config.lc_password}</wsse:Password>
            </wsse:UsernameToken>
            </wsse:Security>
        </soapenv:Header>
    </soapenv:Envelope>
    """
    header = lxml.etree.fromstring(header)[0][0]

    failed_subjects = []
    subject_iteration = 1

    for _, row in df_sparql.iterrows():  # Iterate through subjects
        logger.info(f"Subject iteration number: {subject_iteration}")
        ss_label = row[identifier_colname]
        logger.info(f"Adding subject {ss_label}")

        # Get SS OID
        ss_oid = get_lc_ss_oid(
            lc_endpoint=config.lc_endpoint,
            lc_user=config.lc_user,
            lc_password=config.lc_password,
            study_identifier=study_identifier,
            ss_label=ss_label,
            ss_gender=row[gender_colname],
        )

        logger.info(f"Got SS_OID {ss_oid}")

        event = {
            "studySubjectRef": {"label": ss_label},
            "studyRef": {"identifier": study_identifier},
            "eventDefinitionOID": event_oid,
            "startDate": "2000-01-01",
            "location": "NL",
        }

        # Make sure the event is scheduled: for new subjects (and test), scheduling an event will
        # not be possible
        client = Client(f"{config.lc_endpoint}event/v1/eventWsdl.wsdl")
        with client.settings(strict=False):
            ret = client.service.schedule(event, _soapheaders=[header])

        logger.info(f'Got return code {ret["result"]} for scheduling the event')

        if ret["result"] == "Fail":
            logger.warning(f'Got a non-success code back from LC: {ret["error"]}')

        # Generate the XML for the items to be uploaded based on columns in df.
        # Populate SOAP request with item data to facilitate upload.
        ascii_range = 127
        items = ""
        for name in df_sparql.columns:
            if name != identifier_colname:
                if row[name] is not None and pd.notna(row[name]):
                    if isinstance(row[name], str):
                        if any(ord(char) > ascii_range for char in row[name]):
                            row[name] = unidecode(row[name])
                    items = f'{items}<ItemData ItemOID="{item_prefix}{name}" Value="{row[name]}"/>\n'

        # XML for subject data
        subject_xml = f"""
            <SubjectData SubjectKey="{ss_oid}">
                <StudyEventData StudyEventOID="{event_oid}" StudyEventRepeatKey="1">
                    <FormData FormOID="{form_oid}" OpenClinica:Status="initial data entry">
                        <ItemGroupData ItemGroupOID="{item_group_oid}" ItemGroupRepeatKey="1" TransactionType="Insert">
                            {items}
                        </ItemGroupData>
                    </FormData>
                </StudyEventData>
            </SubjectData>
        """

        logger.info(f"Starting upload for {ss_label}")

        # Password must be hashed to be sent to LC
        submit_data = f"""
            <soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/" xmlns:v1="http://openclinica.org/ws/data/v1" xmlns:OpenClinica="http://www.openclinica.org/ns/odm_ext_v130/v3.1">
                <soapenv:Header>
                    <wsse:Security soapenv:mustUnderstand="1" xmlns:wsse="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd">
                        <wsse:UsernameToken wsu:Id="UsernameToken-27777511" xmlns:wsu="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd">
                            <wsse:Username>{config.lc_user}</wsse:Username>
                            <wsse:Password Type="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-username-token-profile-1.0#PasswordText">{config.lc_password}</wsse:Password>
                        </wsse:UsernameToken>
                    </wsse:Security>
                </soapenv:Header>
                <soapenv:Body>
                    <v1:importRequest>
                        <odm>
                            <ODM>
                                <ClinicalData StudyOID="{study_oid}" MetaDataVersionOID="v1.0.0">
                                <UpsertOn NotStarted="true" DataEntryStarted="true" DataEntryComplete="true"/>
                                {subject_xml}
                                </ClinicalData>
                            </ODM>
                        </odm>
                    </v1:importRequest>
                </soapenv:Body>
            </soapenv:Envelope>
            """

        # Send a POST request to Libre Clinica endpoint
        ret = requests.post(
            f"{config.lc_endpoint}data/v1/dataWsdl.wsdl",
            data=submit_data,
            headers={"SOAPAction": '""', "Content-Type": "text/xml; charset=utf-8"},
            timeout=60,
        )

        logger.info(f"Got return code {ret.status_code} for upload")
        response_text = ret.text

        # Parse response to extract results and errors:
        ret = BeautifulSoup(response_text, features="lxml")
        result = ret.find_all("result")

        if len(result) > 0 and "Success" in result[0].text:
            logger.info("Succeeded in upload:")
            logger.info(f"{result[0].text}")
        else:
            error = ret.find_all("error")
            if error:
                logger.warning(f"Got a non-success code back from LC: {error[0].text}")
                failed_subjects.append({"subject": ss_label, "error": error[0].text})
                logger.info(f"Failed subject: {failed_subjects[-1]}")
            else:
                logger.error(f"No result or error found in response." f" Response: {response_text}\n Exiting now")
                sys.exit(1)

        subject_iteration += 1

    if failed_subjects:
        logger.warning(f"Failed to upload these study subjects: \n" f"{failed_subjects}")

    logger.info(f"Total number of subjects expected: {len(df_sparql)}")
    logger.info(f"Number of failed subjects: {len(failed_subjects)}")


if __name__ == "__main__":
    print("Starting sync!")

    # Environment variables
    LC_ENDPOINT = os.getenv("LC_ENDPOINT")
    LC_USER = os.getenv("LC_USER")
    LC_PASSWORD = os.getenv("LC_PASSWORD")  # Hashed password to Libre Clinica
    SPARQL_QUERY_ENDPOINT = os.environ["SPARQL_QUERY_ENDPOINT"]  # Query endpoint of FAIR station

    filename = "/libre_clinica/upload_config.yaml"

    with open(filename) as f:
        upload_config = yaml.safe_load(f)

    # YAML file variables
    study_oid = upload_config["generic_list"]["study_oid"]
    study_identifier = upload_config["generic_list"]["study_identifier"]
    event_oid = upload_config["generic_list"]["event_oid"]
    form_oid = upload_config["generic_list"]["form_oid"]
    item_group_oid = upload_config["generic_list"]["item_group_oid"]
    identifier_colname = upload_config["generic_list"]["identifier_colname"]
    gender_colname = upload_config["generic_list"]["gender_colname"]
    item_prefix = upload_config["generic_list"]["item_prefix"]
    query = upload_config["generic_list"]["query"]

    libre_clinica_config = LCConfig(
        sparql_endpoint=SPARQL_QUERY_ENDPOINT,
        query=query,
        lc_endpoint=LC_ENDPOINT,
        lc_user=LC_USER,
        lc_password=LC_PASSWORD,
        study_oid=study_oid,
        study_identifier=study_identifier,
        event_oid=event_oid,
        form_oid=form_oid,
        item_group_oid=item_group_oid,
        identifier_colname=identifier_colname,
        gender_colname=gender_colname,
        item_prefix=item_prefix,
        alternative_item_oids=None,
        perform_query=True,
    )

    upload_to_lc(libre_clinica_config)

    print("Finished sync!")
