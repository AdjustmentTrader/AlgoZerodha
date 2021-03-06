import logging
from datetime import datetime
from multiprocessing import synchronize
from pickle import FALSE, TRUE
from tkinter import E

from instruments.Instruments import Instruments
from models.Direction import Direction
from models.ProductType import ProductType
from strategies.BaseStrategy import BaseStrategy
from utils.Utils import Utils
from trademgmt.Trade import Trade
from trademgmt.TradeManager import TradeManager
import threading
import random

# Each strategy has to be derived from BaseStrategy
class ShortStraddleBNF(BaseStrategy):
  __instance = None
  flag = "no"

  @staticmethod
  def getInstance(): # singleton class
    if ShortStraddleBNF.__instance == None:
      ShortStraddleBNF()
    return ShortStraddleBNF.__instance

  def __init__(self):
    if ShortStraddleBNF.__instance != None:
      raise Exception("This class is a singleton!")
    else:
      ShortStraddleBNF.__instance = self
    # Call Base class constructor
    super().__init__("ShortStraddleBNF")
    # Initialize all the properties specific to this strategy
    self.productType = ProductType.MIS
    self.symbols = []
    self.slPercentage = 30
    self.targetPercentage = 0
    self.startTimestamp = Utils.getTimeOfToDay(9, 15, 20) # When to start the strategy. Default is Market start time
    self.stopTimestamp = Utils.getTimeOfToDay(15, 0, 0) # This is not square off timestamp. This is the timestamp after which no new trades will be placed under this strategy but existing trades continue to be active.
    self.squareOffTimestamp = Utils.getTimeOfToDay(15, 24, 0) # Square off time
    self.capital = 100000 # Capital to trade (This is the margin you allocate from your broker account for this strategy)
    self.leverage = 0
    self.maxTradesPerDay = 2 # (1 CE + 1 PE) Max number of trades per day under this strategy
    self.isFnO = True # Does this strategy trade in FnO or not
    self.capitalPerSet = 105000 # Applicable if isFnO is True (1 set means 1CE/1PE or 2CE/2PE etc based on your strategy logic)
    logging.error("BN STRADDLE STARTING")
    self.process()

  def canTradeToday(self):
    # Even if you remove this function canTradeToday() completely its same as allowing trade every day
    logging.error("BN STRADDLE STARTING START")
    return True

  def process(self):
    now = datetime.now()
    if now < self.startTimestamp:
      return
    if len(self.trades) >= self.maxTradesPerDay:
      return
    # Get current market price of Nifty Future
    futureSymbol = Utils.prepareMonthlyExpiryFuturesSymbol('NIFTY')
    quote = self.getQuote(futureSymbol)
    if quote == None:
      logging.error('%s: Could not get quote for %s', self.getName(), futureSymbol)
      return

    ATMStrike = Utils.getNearestStrikePrice(quote.lastTradedPrice, 50)
    logging.info('%s: Nifty CMP = %f, ATMStrike = %d', self.getName(), quote.lastTradedPrice, ATMStrike)
    ATMCESymbol = Utils.prepareWeeklyOptionsSymbol("NIFTY", ATMStrike, 'CE')
    ATMPESymbol = Utils.prepareWeeklyOptionsSymbol("NIFTY", ATMStrike, 'PE')
    logging.info('%s: ATMCESymbol = %s, ATMPESymbol = %s', self.getName(), ATMCESymbol, ATMPESymbol)
    #create trades
    self.generateTrades(ATMCESymbol, ATMPESymbol)

  def generateTrades(self, ATMCESymbol, ATMPESymbol):
    numLots = self.calculateLotsPerTrade()
    quoteATMCESymbol = self.getQuote(ATMCESymbol)
    quoteATMPESymbol = self.getQuote(ATMPESymbol)
    if quoteATMCESymbol == None or quoteATMPESymbol == None:
      logging.error('%s: Could not get quotes for option symbols', self.getName())
      return

    self.generateTrade(ATMCESymbol, numLots, quoteATMCESymbol.lastTradedPrice)
    self.generateTrade(ATMPESymbol, numLots, quoteATMPESymbol.lastTradedPrice)
    logging.error('%s: Trades generated.', self.getName())

  def generateTrade(self, optionSymbol, numLots, lastTradedPrice):
    trade = Trade(optionSymbol)
    trade.strategy = self.getName()
    trade.isOptions = True
    trade.direction = Direction.SHORT # Always short here as option selling only
    trade.productType = self.productType
    trade.placeMarketOrder = True
    trade.requestedEntry = lastTradedPrice
    trade.timestamp = Utils.getEpoch(self.startTimestamp) # setting this to strategy timestamp
    
    isd = Instruments.getInstrumentDataBySymbol(optionSymbol) # Get instrument data to know qty per lot
    #trade.qty = isd['lot_size'] * numLots
    trade.qty = 300
    
    #trade.stopLoss = Utils.roundToNSEPrice(trade.requestedEntry + trade.requestedEntry * self.slPercentage / 100)
    #trade.target = 0 # setting to 0 as no target is applicable for this trade

    trade.intradaySquareOffTimestamp = Utils.getEpoch(self.squareOffTimestamp)
    # Hand over the trade to TradeManager
    TradeManager.addNewTrade(trade)

  def shouldPlaceTrade(self, trade, tick):
    # First call base class implementation and if it returns True then only proceed
    if super().shouldPlaceTrade(trade, tick) == False:
      return False
    # We dont have any condition to be checked here for this strategy just return True
    return True

  def adjustment(self,price):
    try:
      lock = threading.Lock()
      lock.acquire()
      strike = set()
      strikeprice = None
      upSide = None
      downSide = None
      for tr in TradeManager.trades:
        if tr.tradeState == "active":
          strike.add(str(tr.tradingSymbol)[10:15])
      #To do###
      # ###Scenario 1
      if (len(strike) == 1):
        strikeprice = strike.pop()
        if price != None and strikeprice != None:
          upSide = int(strikeprice) + 61
          downSide = int(strikeprice) - 61
        #if (int("17210") >= upSide):
        if (int(float(price)) >= upSide) and ShortStraddleBNF.flag == "no":
          ShortStraddleBNF.flag = "yes"
          sell = Utils.prepareWeeklyOptionsSymbol("NIFTY", int(strikeprice)+50, 'CE') ##sell 
          buy = Utils.prepareWeeklyOptionsSymbol("NIFTY", int(strikeprice), 'CE') ##buy    
          self.generateAdjustmentTrades(buy, sell, price)
        #if (int("17010") <= downSide):
        if (int(float(price)) <= downSide) and ShortStraddleBNF.flag == "no":
          ShortStraddleBNF.flag = "yes"
          sell = Utils.prepareWeeklyOptionsSymbol("NIFTY", int(strikeprice)-50, 'PE') ##sell 
          buy = Utils.prepareWeeklyOptionsSymbol("NIFTY", int(strikeprice), 'PE')   ##buy
          self.generateAdjustmentTrades(buy, sell, price)
      elif (len(strike) == 2):
        one = strike.pop()
        two = strike.pop()
        ce = None
        pe = None
        if one > two:
          ce = one
          pe = two
        else:
          ce = two
          pe = one
        if price != None and ce != None and pe != None:
          upSide = int(ce) + 26
          downSide = int(pe) - 26
        if (int(float(price)) >= upSide) and ShortStraddleBNF.flag == "yes":
          ShortStraddleBNF.flag = "no"
          buy = Utils.prepareWeeklyOptionsSymbol("NIFTY", int(pe), 'PE') ##buy
          sell = Utils.prepareWeeklyOptionsSymbol("NIFTY", int(pe)+50, 'PE') ##sell
          self.generateAdjustmentTrades(buy, sell, price)   
        if (int(float(price)) <= downSide) and ShortStraddleBNF.flag == "yes":
          ShortStraddleBNF.flag = "no"
          buy = Utils.prepareWeeklyOptionsSymbol("NIFTY", int(ce), 'CE') ##buy
          sell = Utils.prepareWeeklyOptionsSymbol("NIFTY", int(ce)-50, 'CE') ##sell
          self.generateAdjustmentTrades(buy, sell, price)
      else:
        logging.error('No trade to adjustment')
      lock.release()
    except Exception as e:
      print(str(e))
    return True

  def generateAdjustmentTrades(self, buy, sell, price):
    numLots = self.calculateLotsPerTrade()
    closeTrade = self.getQuote(buy)
    openTrade = self.getQuote(sell)
    if closeTrade == None or openTrade == None:
      logging.error('%s: Could not get quotes for option symbols', self.getName())
      return

    self.generateAdjustmentTrade(buy, numLots, closeTrade.lastTradedPrice, Direction.LONG, price)
    self.generateAdjustmentTrade(sell, numLots, openTrade.lastTradedPrice, Direction.SHORT, price)
    logging.error('%s: Trades generated.', self.getName())

  def generateAdjustmentTrade(self, optionSymbol, numLots, lastTradedPrice, direction, price):
    trade = Trade(optionSymbol)
    trade.strategy = self.getName()
    trade.isOptions = True
    trade.direction = direction # Always short here as option selling only
    trade.productType = self.productType
    trade.placeMarketOrder = True
    trade.requestedEntry = lastTradedPrice
    trade.timestamp = Utils.getEpoch(self.startTimestamp) # setting this to strategy timestamp
    
    isd = Instruments.getInstrumentDataBySymbol(optionSymbol) # Get instrument data to know qty per lot
    #trade.qty = isd['lot_size'] * numLots
    trade.qty = 300
    
    # trade.stopLoss = Utils.roundToNSEPrice(trade.requestedEntry + trade.requestedEntry * self.slPercentage / 100)
    # trade.target = 0 # setting to 0 as no target is applicable for this trade

    trade.intradaySquareOffTimestamp = Utils.getEpoch(self.squareOffTimestamp)
    # Hand over the trade to TradeManager
    # for tr in TradeManager.trades:
    #   if tr.tradingSymbol == trade.tradingSymbol and tr.strategy == trade.strategy and tr.direction == trade.direction and trade.tradeState == "created":
    #     print(str(trade.tradingSymbol)+"----"+str(price)+"----"+str(trade.direction)+"-----"+str(trade.tradeState)+"-----"+str(tr.tradeState)+"-----"+str(tr.direction))          
    #     # if tr.tradeState == "disabled":
    #     #   TradeManager.trades.append(trade)
    #     return 0
    #   # else:
    #   #   print(str(trade.tradingSymbol)+"--ELSE---"+str(trade.direction)+"--ELSE---"+str(trade.tradeState)+"--ELSE---"+str(tr.tradeState)+"-ELSE----"+str(tr.direction))

    TradeManager.addNewTrade(trade)

  def getTrailingSL(self, trade):
    if trade == None:
      return 0
    if trade.entry == 0:
      return 0
    lastTradedPrice = TradeManager.getLastTradedPrice(trade.tradingSymbol)
    if lastTradedPrice == 0:
      return 0

    trailSL = 0
    profitPoints = int(trade.entry - lastTradedPrice)
    if profitPoints >= 5:
      factor = int(profitPoints / 5)
      trailSL = Utils.roundToNSEPrice(trade.initialStopLoss - factor * 5)
    logging.info('%s: %s Returning trail SL %f', self.getName(), trade.tradingSymbol, trailSL)
    return trailSL

