"""This Python script contains a class that executes a SPARQL query and stores its results in a
pandas dataframe.
"""

# pylint: disable = E0401

import logging
import os
from typing import Type

import numpy as np
import pandas as pd
import requests
from requests.auth import HTTPBasicAuth

logger = logging.getLogger(__name__)


class QueryEngine:
    """This class contains functions that (1) read a SPARQL query
    and (2) execute a SPARQL query and store its results in a pandas
    dataframe.
    """

    def __init__(self, service_location: str):
        """Service location is the location of the SPARQL endpoint."""
        self.__service_location = service_location

    @staticmethod
    def convert(column: str, cast_to: Type):
        """To handle type conversions of dataframe columns

        Args:
            column: column to attempt to convert
            cast_to: the value you want your column to be (for example: int, float, etc.)

        Returns:
            The column converted to the intended format. If this fails, then a missing value is
            assigned.
        """
        if column is not None:
            try:
                tmp = cast_to(column)
            except ValueError:
                tmp = np.NaN
        else:
            tmp = np.NaN
        return tmp

    def query_from_file(self, file_name: str):
        """This function reads a file containing a SPARQL query and calls the
        'get_sparql_dataframe' function to execute a SPARQL query and stores its
        results into a pandas dataframe.
        """
        with open(file_name, "r") as file:
            query = file.read().replace("\n", " ")
        return self.get_sparql_dataframe(query)

    def get_sparql_dataframe(self, query: str, auth_required: bool = True):
        """This function executes a SPARQL query and stores its results in a
        pandas dataframe.
        """
        query_endpoint = os.getenv("SPARQL_QUERY_ENDPOINT")  # type: ignore

        if auth_required:
            # Here provide username, and password of your FAIR data station.
            username = os.getenv("USER")
            password = os.getenv("USER_PWD")

            # Post the upload_config.yaml file query to the SPARQL endpoint.
            result = requests.post(
                query_endpoint, auth=HTTPBasicAuth(username, password), data={"query": query}, timeout=60  # type: ignore
            )
        else:
            # Post the upload_config.yaml file query to the SPARQL endpoint.
            result = requests.post(query_endpoint, data={"query": query}, timeout=60)  # type: ignore
        logger.info(f"Received status code: {result.status_code}")

        # Retrieve the relevant results from the json format.
        processed_results = result.json()
        columns = processed_results["head"]["vars"]

        out = []
        for row in processed_results["results"]["bindings"]:
            item = []
            for col in columns:
                item.append(row.get(col, {}).get("value"))
            out.append(item)  # Each row is a list of values corresponding to the columns.

        # Store relevant results in a data frame.
        df_sparql = pd.DataFrame(out, columns=columns)

        # Change the column types of the results dataframe.
        if len(processed_results["results"]["bindings"]) > 0:
            first_row = processed_results["results"]["bindings"][0]  # The first row is inspected
            # to determine the types of the columns.
            literal_variable_types = {"literal", "typed-literal"}
            integer_data_types = {
                "http://www.w3.org/2001/XMLSchema#int",
                "http://www.w3.org/2001/XMLSchema#integer",
            }
            for col in columns:
                var_type = first_row.get(col, {}).get("type")
                if var_type == "uri":
                    df_sparql[col] = df_sparql[col].astype("category")
                if var_type in literal_variable_types:
                    data_type = first_row.get(col, {}).get("datatype")
                    if data_type in integer_data_types:
                        df_sparql[col] = df_sparql[col].apply(lambda x: self.convert(x, int)).astype("Int64")
                    if data_type == "http://www.w3.org/2001/XMLSchema#double":
                        df_sparql[col] = df_sparql[col].apply(lambda x: self.convert(x, float)).astype("Float64")
                    if data_type == "http://www.w3.org/2001/XMLSchema#string":
                        df_sparql[col] = df_sparql[col].astype("category")

        return df_sparql
