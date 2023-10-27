__author__ = "MarkusDoepfert"
__credits__ = ""
__license__ = ""
__maintainer__ = "MarkusDoepfert"
__email__ = "markus.doepfert@tum.de"

# Imports
import os
import pandas as pd
import polars as pl
import numpy as np
import time
import logging
import traceback
from datetime import datetime
import hamlet.constants as c
from hamlet.executor.utilities.database.market_db import MarketDB
from hamlet.executor.utilities.database.region_db import RegionDB
from hamlet.executor.utilities.database.database import Database
from pprint import pprint

# TODO: Considerations
# - Each timestep is a new instance of the lem
# - The commands are executed in the order of the methods


class Lem:

    def __init__(self, market: MarketDB, tasks: dict, database: Database):
        # Market database
        self.market = market

        # Tasklist
        self.tasks = tasks

        # Database
        self.database = database

        # Get bids and offers
        self.bids_offers = self.database.get_bids_offers(region=self.tasks[c.TC_REGION],
                                                         market_type=self.tasks[c.TC_MARKET],
                                                         market_name=self.tasks[c.TC_NAME],
                                                         timestep=self.tasks[c.TC_TIMESTEP])

        # Get the tables from the market database and clear them
        self.bids_cleared = self.market.bids_cleared.clear()
        self.offers_cleared = self.market.offers_cleared.clear()
        self.bids_uncleared = self.market.bids_uncleared.clear()
        self.offers_uncleared = self.market.offers_uncleared.clear()
        self.transactions = self.market.market_transactions.clear()

        # Get the retailer offers
        self.retailer = self.market.retailer.filter(pl.col(c.TC_TIMESTAMP) == self.tasks[c.TC_TIMESTEP])

        # Available actions (see market config)
        self.actions = {
            'clear': self.__action_clear,
            'settle': self.__action_settle,
        }

        # Available clearing types (see market config)
        self.types = {
            None: {},  # no clearing (ponder if even part of it or just, if None, then just a wholesale market)
            'ex-ante': self.__type__ex_ante,
            'ex-post': self.__type_ex_post,
        }

        # Available clearing methods (see market config)
        self.methods = {
            'pda': self.__method_pda,  # periodic double auction
            'community': self.__method_community,  # community-based clearing
        }

        # Available pricing methods (see market config)
        self.pricing = {
            'uniform': self.__pricing_uniform,  # uniform pricing
            'discriminatory': self.__pricing_discriminatory,  # discriminatory pricing
        }

        # Available coupling methods (see market config)
        # Note: This probably means that the upper market draws the offers and bids from the lower market (ponder)
        # TODO: This needs to change. The creator will either have a value or not there and the market just executes.
        #  In its current form there would be a more functionality in the executor that should be in the creator.
        self.coupling = {
            None: self.__return_data,  # no coupling
            'above': self.__coupling_above,  # post offers and bids on market above
            'below': self.__coupling_below,  # post offers and bids on market below
        }

    def execute(self):
        """Executes all the actions of the LEM defined in the tasks"""

        # Generated with co-pilot so might be not quite right
        # Get the actions to be executed
        actions = self.tasks[c.TC_ACTIONS].split(',')
        # Get the clearing type
        clearing_type = self.tasks[c.TC_CLEARING_TYPE]
        # Get the clearing method
        clearing_method = self.tasks[c.TC_CLEARING_METHOD]
        # Get the pricing method
        pricing_method = self.tasks[c.TC_CLEARING_PRICING]
        # Get the coupling method
        coupling_method = self.tasks[c.TC_COUPLING]

        # Execute the actions
        for action in actions:
            print(action)
            self.actions[action](clearing_type, clearing_method, pricing_method, coupling_method)

        # Couple market
        # Note: This is not part of the actions, but is executed after the actions
        self.couple_markets(clearing_type, clearing_method, pricing_method, coupling_method)

        # Update the market database
        self.market.bids_cleared = self.bids_cleared
        self.market.offers_cleared = self.offers_cleared
        self.market.bids_uncleared = self.bids_uncleared
        self.market.offers_uncleared = self.offers_uncleared
        self.market.market_transactions = self.market.market_transactions.append(self.transactions)

        print('We got here.')
        exit()

        return self.market

    def __action_clear(self, clearing_type, clearing_method, pricing_method, coupling_method, **kwargs):
        """Clears the market

        Note that if the markets are coupled there might already be postings that need to be included (but then again they should be posted by the previous market so might be irrelevant)
        """
        # TODO: For now practically ignores all the parameters and just clears the market. Needs to change.

        # Definition of temporary column names
        c_energy_cumsum = 'energy_cumsum'

        # Check if there is anything to clear otherwise return
        if self.bids_offers.collect().is_empty():
            return (self.transactions, self.offers_uncleared, self.bids_uncleared, self.offers_cleared,
                    self.bids_cleared)

        start = time.perf_counter()
        # Add bid and offer by the retailers
        retailer = self.retailer.select(pl.col(c.TC_TIMESTAMP), pl.col(c.TC_REGION), pl.col(c.TC_MARKET),
                                        pl.col(c.TC_NAME), pl.col('retailer'),
                                        pl.col('energy_price_sell'), pl.col('energy_price_buy'),
                                        pl.col('energy_quantity_sell'), pl.col('energy_quantity_buy'))
        retailer = retailer.with_columns(
            [
                pl.col(c.TC_TIMESTAMP).alias(c.TC_TIMESTEP),
                pl.lit(None).alias(c.TC_ENERGY_TYPE),  # TODO: This can be removed once the energy type is added to the retailer table
            ]
        )
        # TODO: Some of those will not need renaming in the future as the retailer table is changed
        retailer = retailer.rename({'retailer': c.TC_ID_AGENT,
                                    'energy_price_sell': c.TC_PRICE_PU_IN, 'energy_price_buy': c.TC_PRICE_PU_OUT,
                                    'energy_quantity_sell': c.TC_ENERGY_IN, 'energy_quantity_buy': c.TC_ENERGY_OUT})

        retailer = retailer.with_columns(
            [
                pl.col(c.TC_REGION).cast(pl.Categorical, strict=False),
                pl.col(c.TC_MARKET).cast(pl.Categorical, strict=False),
                pl.col(c.TC_NAME).cast(pl.Categorical, strict=False),
                pl.col(c.TC_ENERGY_TYPE).cast(pl.Utf8, strict=False),
                pl.col(c.TC_PRICE_PU_IN).cast(pl.Int32, strict=False),
                pl.col(c.TC_PRICE_PU_OUT).cast(pl.Int32, strict=False),
            ]
        )
        #print(self.bids_offers.columns)
        #retailer = retailer.collect()
        retailer = retailer.select(self.bids_offers.columns)
        #retailer = retailer(schema=self.bids_offers.schema)
        #bids_offers = self.bids_offers.collect().vstack(retailer)
        bids_offers = pl.concat([self.bids_offers, retailer], how='align')

        with pl.Config() as cfg:
           cfg.set_tbl_width_chars(400)
           cfg.set_tbl_cols(25)
           print(self.bids_offers.collect())
           print(retailer.collect())
           print(bids_offers.collect())
        exit()

        # Split the bids and offers into separate bids and offers tables
        bids = bids_offers.filter(pl.col(c.TC_ENERGY_IN) > 0)
        offers = bids_offers.filter(pl.col(c.TC_ENERGY_OUT) > 0)

        # Drop the respective empty columns
        bids = bids.drop(c.TC_ENERGY_OUT, c.TC_PRICE_PU_OUT)
        offers = offers.drop(c.TC_ENERGY_IN, c.TC_PRICE_PU_IN)

        # Rename agent column
        bids = bids.rename({c.TC_ID_AGENT: c.TC_ID_AGENT_IN})
        offers = offers.rename({c.TC_ID_AGENT: c.TC_ID_AGENT_OUT})

        # Shuffle the data to avoid bias
        bids = bids.collect().sample(fraction=1, shuffle=True)
        offers = offers.collect().sample(fraction=1, shuffle=True)

        # Sort the bids and offers by price
        bids = bids.sort(c.TC_PRICE_PU_IN, descending=True)
        offers = offers.sort(c.TC_PRICE_PU_OUT, descending=False)

        # Add column that contains the cumsum of the energy
        bids = bids.with_columns(
            [
                pl.col(c.TC_ENERGY_IN).cumsum().alias('energy_cumsum'),
            ]
        )
        offers = offers.with_columns(
            [
                pl.col(c.TC_ENERGY_OUT).cumsum().alias('energy_cumsum'),
            ]
        )

        # Merge bids and offers on the energy_cumsum column
        # TODO: Might need suffixes
        bids_offers = bids.join(offers, on='energy_cumsum', how='outer').lazy()

        # Sort the bids and offers by the energy_cumsum
        bids_offers = bids_offers.sort('energy_cumsum', descending=False)#.fill_null(strategy='backward')

        # Remove all columns that end on _right
        double_cols = [col for col in bids_offers.columns if col.endswith('_right')]
        for col in double_cols:
            orig_col = col.rsplit('_', 1)[0]
            bids_offers = bids_offers.with_columns(pl.coalesce([orig_col, col]).alias(orig_col))
        bids_offers = bids_offers.drop(double_cols)

        # Fill the NaN values with the last value
        bids_offers = bids_offers.fill_null(strategy='backward')

        # Create new dataframe with the cleared bids and offers
        trades_cleared = bids_offers.filter(pl.col(c.TC_PRICE_PU_IN) >= pl.col(c.TC_PRICE_PU_OUT))

        # Calculate the pu price of the trades
        trades_cleared = self.pricing[pricing_method](trades_cleared)

        # Calculate the price and energy of the trades
        trades_cleared = trades_cleared.with_columns(
            (trades_cleared.select([c.TC_ENERGY_IN, c.TC_ENERGY_OUT]).collect().min(axis=1).alias(c.TC_ENERGY)),
        )
        trades_cleared = trades_cleared.with_columns(
            (pl.col(c.TC_PRICE_PU) * pl.col(c.TC_ENERGY)).alias(c.TC_PRICE),
        )

        # Make trades_cleared a dataframe
        trades_cleared = trades_cleared.collect()

        # Create new dataframe with the uncleared bids and offers
        # Note: this tables includes the trades that were not cleared and the ones that were only partially cleared
        trades_uncleared = bids_offers.filter(pl.col(c.TC_PRICE_PU_IN) < pl.col(c.TC_PRICE_PU_OUT))

        with pl.Config() as cfg:
            cfg.set_tbl_width_chars(400)
            cfg.set_tbl_cols(20)
            cfg.set_tbl_rows(20)
            print(bids)
            print(offers)
            print(trades_cleared)

        # Create the cleared tables
        # Filter out the bids and offers that were cleared by checking for the same agent id
        bids_cleared = bids.join(trades_cleared, on=c.TC_ID_AGENT_IN, how='semi')
        offers_cleared = offers.join(trades_cleared, on=c.TC_ID_AGENT_OUT, how='semi')

        # Add the energy, price pu and price column from the trades_cleared table
        cols = [c.TC_ID_AGENT_IN, c.TC_ENERGY, c.TC_PRICE_PU, c.TC_PRICE]
        bids_cleared = bids_cleared.join(trades_cleared.select(cols), on=c.TC_ID_AGENT_IN, how='inner')
        cols = [c.TC_ID_AGENT_OUT, c.TC_ENERGY, c.TC_PRICE_PU, c.TC_PRICE]
        offers_cleared = offers_cleared.join(trades_cleared.select(cols), on=c.TC_ID_AGENT_OUT, how='inner')

        # Drop the unnecessary columns and rename the relevant ones to create the final tables
        bids_cleared = bids_cleared.drop(c.TC_ENERGY_IN, c.TC_PRICE_PU_IN, 'energy_cumsum')
        bids_cleared = bids_cleared.rename({c.TC_ENERGY: c.TC_ENERGY_IN, c.TC_PRICE_PU: c.TC_PRICE_PU_IN,
                                            c.TC_PRICE: c.TC_PRICE_IN})
        offers_cleared = offers_cleared.drop(c.TC_ENERGY_OUT, c.TC_PRICE_PU_OUT, 'energy_cumsum')
        offers_cleared = offers_cleared.rename({c.TC_ENERGY: c.TC_ENERGY_OUT, c.TC_PRICE_PU: c.TC_PRICE_PU_OUT,
                                                c.TC_PRICE: c.TC_PRICE_OUT})

        # Create the uncleared tables
        # First take all bids and offers
        bids_uncleared = bids
        offers_uncleared = offers

        # Subtract the cleared bids and offers energy amount by agent id
        # Bids
        # First get the sum of the cleared energy by agent id
        bids_cleared_by_agent_id = bids_cleared.groupby(c.TC_ID_AGENT_IN).sum()
        bids_cleared_by_agent_id = bids_cleared_by_agent_id.rename({c.TC_ENERGY_IN: c.TC_ENERGY})
        bids_cleared_by_agent_id = bids_cleared_by_agent_id.select([c.TC_ID_AGENT_IN, c.TC_ENERGY])
        # Join the dataframes to have the information about the energy that was cleared
        bids_uncleared = bids_uncleared.join(bids_cleared_by_agent_id, on=c.TC_ID_AGENT_IN, how='outer')
        # Set all null values in the energy column to 0
        bids_uncleared = bids_uncleared.fill_null(0)
        # Subtract the cleared energy from the uncleared energy
        bids_uncleared = bids_uncleared.with_columns(
            (pl.col(c.TC_ENERGY_IN) - pl.col(c.TC_ENERGY)).alias(c.TC_ENERGY_IN),
        )
        # Drop the rows where the energy is smaller or equal to 0
        bids_uncleared = bids_uncleared.filter(pl.col(c.TC_ENERGY_IN) > 0)
        # Drop the energy and energy_cumsum column
        bids_uncleared = bids_uncleared.drop(c.TC_ENERGY, 'energy_cumsum')
        # Drop all rows where the agent id is the same as the one in the retailer table
        retailer_names = retailer.select(c.TC_ID_AGENT).collect().to_series().to_list()
        bids_uncleared = bids_uncleared.filter(~pl.col(c.TC_ID_AGENT_IN).is_in(retailer_names))
        # Offers
        # First get the sum of the cleared energy by agent id
        offers_cleared_by_agent_id = offers_cleared.groupby(c.TC_ID_AGENT_OUT).sum()
        offers_cleared_by_agent_id = offers_cleared_by_agent_id.rename({c.TC_ENERGY_OUT: c.TC_ENERGY})
        offers_cleared_by_agent_id = offers_cleared_by_agent_id.select([c.TC_ID_AGENT_OUT, c.TC_ENERGY])
        # Join the dataframes to have the information about the energy that was cleared
        offers_uncleared = offers_uncleared.join(offers_cleared_by_agent_id, on=c.TC_ID_AGENT_OUT, how='outer')
        # Set all null values in the energy column to 0
        offers_uncleared = offers_uncleared.fill_null(0)
        # Subtract the cleared energy from the uncleared energy
        offers_uncleared = offers_uncleared.with_columns(
            (pl.col(c.TC_ENERGY_OUT) - pl.col(c.TC_ENERGY)).alias(c.TC_ENERGY_OUT),
        )
        # Drop the rows where the energy is smaller or equal to 0
        offers_uncleared = offers_uncleared.filter(pl.col(c.TC_ENERGY_OUT) > 0)
        # Drop the energy and energy_cumsum column
        offers_uncleared = offers_uncleared.drop(c.TC_ENERGY, 'energy_cumsum')
        # Drop all rows where the agent id is the same as the one in the retailer table
        offers_uncleared = offers_uncleared.filter(~pl.col(c.TC_ID_AGENT_OUT).is_in(retailer_names))

        # with pl.Config() as cfg:
        #     cfg.set_tbl_width_chars(400)
        #     cfg.set_tbl_cols(20)
        #     cfg.set_tbl_rows(20)
        #     print(bids_cleared)
        #     #print(bids_cleared_by_agent_id)
        #     print(bids_uncleared)
        #     print(retailer.collect())
        #     print(offers_cleared)
        #     #print(offers_cleared_by_agent_id)
        #     print(offers_uncleared)
        #     #print(bids_offers.collect())
        #     #print(trades_cleared.collect())
        #     #print(trades_uncleared.collect())
        #     #print(merged)
        # print(f'Time: {time.perf_counter() - start}')
        # exit()

        # TODO: Reduce the available energy of the retailer by the amount that was bought/sold to them

        # Add the trades to their corresponding tables
        self.bids_cleared = pl.concat([self.bids_cleared, bids_cleared], how='align')
        self.offers_cleared = pl.concat([self.offers_cleared, offers_cleared], how='align')
        self.bids_uncleared = pl.concat([self.bids_uncleared, bids_uncleared], how='align')
        self.offers_uncleared = pl.concat([self.offers_uncleared, offers_uncleared], how='align')
        self.transactions = pl.concat([self.transactions, trades_cleared], how='align')

        with pl.Config() as cfg:
            cfg.set_tbl_width_chars(400)
            cfg.set_tbl_cols(20)
            cfg.set_tbl_rows(20)
            print(self.bids_cleared.collect())
            print(self.offers_cleared.collect())
            print(self.bids_uncleared.collect())
            print(self.offers_uncleared.collect())
            print(self.transactions.collect())
        print(f'Time: {time.perf_counter() - start}')
        exit()

        # TODO: Probably needs to append the transaction to the market right away

        # TODO: Reduce/Increase the available energy of the retailer

        return self.bids_cleared, self.offers_cleared, self.bids_uncleared, self.offers_uncleared, self.transactions

    def __action_settle(self, clearing_type, clearing_method, pricing_method, coupling_method, **kwargs):
        """Settles the market"""
        # TODO: At this point the trades that occured get settled thus balancing energy is determined
        #  as well as levies and taxes are applied

        # TODO: This needs to be different probably. Here it needs to look at all the trades and not just the one at that instance.

        # TODO: Get all trades for the timestamp

        # TODO: Determine balancing energy
        self.transaction = self.determine_balancing_energy(self.transactions)

        # TODO: Apply levies and taxes
        self.apply_levies_taxes()

        return self.bids_cleared, self.offers_cleared, self.bids_uncleared, self.offers_uncleared, self.transactions

    def couple_markets(self, clearing_type, clearing_method, pricing_method, coupling_method, **kwargs):
        """Couple the market"""
        # This will probably mean that the uncleared bids and offers will be changed to a different market so that they can be cleared there.
        # Note that this means that they need to consider different pricing though
        # Executed with the unsettled bids and offers, if any exist and coupling method to be done
        ...

    def determine_balancing_energy(self, **kwargs):
        """Determines the balancing energy"""
        ...

    def post_results(self, **kwargs):
        """Posts the results to the database"""
        ...

    def __type__ex_ante(self):
        """Clears the market ex-ante"""
        ...

    def __type_ex_post(self):
        """Clears the market ex-post"""
        ...

    def __method_pda(self):
        """Clears the market with the periodic double auction method"""
        ...

    def __method_community(self):
        """Clears the market with the community-based clearing method"""
        ...

    def __pricing_uniform(self, trades):
        """Prices the market with the uniform pricing method, thus everyone gets the same price which is the average
        of the last value of the price_pu_in and price_pu_out"""

        # Price PU column: average of the last value of the price_pu_in and price_pu_out
        trades = trades.with_columns(
                ((pl.col(c.TC_PRICE_PU_OUT).tail() + pl.col(c.TC_PRICE_PU_IN).tail()) / 2)
                .round().cast(pl.Int32).alias(c.TC_PRICE_PU),
        )

        return trades

    def __pricing_discriminatory(self):
        """Prices the market with the discriminatory pricing method"""
        # OLD
        # Calculate discriminative prices if demanded
        #if 'discriminatory' == config_lem['types_pricing_ex_ante'][i]:
        #    positions_cleared.loc[:, db_obj.db_param.PRICE_ENERGY_MARKET_ + type_pricing] = \
        #        ((positions_cleared[db_obj.db_param.PRICE_ENERGY_OFFER] +
        #          positions_cleared[db_obj.db_param.PRICE_ENERGY_BID].iloc[:]) / 2).astype(int)
        ...

    def __coupling_above(self):
        """Coupling with the market above"""
        ...

    def __coupling_below(self):
        """Coupling with the market below"""
        ...

    def apply_levies_taxes(self):
        """Applies levies and taxes to the market"""
        # Needs to discriminate between the different types of levies and taxes (wholesale or local)
        ...

    @staticmethod
    def __return_data(data):
        return data
