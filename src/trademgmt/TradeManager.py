import os
import logging
import time
import json
from datetime import datetime
import telegram_send

from config.Config import getServerConfig
from core.Controller import Controller
from ticker.ZerodhaTicker import ZerodhaTicker
from trademgmt.Trade import Trade
from trademgmt.TradeState import TradeState
from trademgmt.TradeExitReason import TradeExitReason
from trademgmt.TradeEncoder import TradeEncoder
from ordermgmt.ZerodhaOrderManager import ZerodhaOrderManager
from ordermgmt.OrderInputParams import OrderInputParams
from ordermgmt.OrderModifyParams import OrderModifyParams
from ordermgmt.Order import Order
from models.OrderType import OrderType
from models.OrderStatus import OrderStatus
from models.Direction import Direction
import requests
from copy import copy
from core.Quotes import Quotes

from utils.Utils import Utils

class TradeManager:
  ticker = None
  trades = [] # to store all the trades
  strategyToInstanceMap = {}
  symbolToCMPMap = {}
  intradayTradesDir = None
  registeredSymbols = []
  directionDict = {}

  @staticmethod
  def run():
    if Utils.isTodayHoliday():
      logging.info("Cannot start TradeManager as Today is Trading Holiday.")
      return
    if Utils.isMarketClosedForTheDay():
      logging.info("Cannot start TradeManager as Market is closed for the day.")
      return

    Utils.waitTillMarketOpens("TradeManager")
    

    # check and create trades directory for today`s date
    serverConfig = getServerConfig()
    tradesDir = os.path.join(serverConfig['deployDir'], 'trades')
    TradeManager.intradayTradesDir =  os.path.join(tradesDir, Utils.getTodayDateStr())
    if os.path.exists(TradeManager.intradayTradesDir) == False:
      logging.info('TradeManager: Intraday Trades Directory %s does not exist. Hence going to create.', TradeManager.intradayTradesDir)
      os.makedirs(TradeManager.intradayTradesDir)

    # start ticker service
    brokerName = Controller.getBrokerName()
    if brokerName == "zerodha":
      TradeManager.ticker = ZerodhaTicker()
    #elif brokerName == "fyers" # not implemented
    # ticker = FyersTicker()

    TradeManager.ticker.startTicker()
    TradeManager.ticker.registerListener(TradeManager.tickerListener)

    # sleep for 2 seconds for ticker connection establishment
    time.sleep(2)

    # Load all trades from json files to app memory
    TradeManager.loadAllTradesFromFile()
    stopLoss = -25
    dict = {}
    lastPriceDict = {}
    previoustotal = 0
    # track and update trades in a loop
    while True:
      if Utils.isMarketClosedForTheDay():
        logging.info('TradeManager: Stopping TradeManager as market closed.')
        break
      try:
        # Fetch all order details from broker and update orders in each trade
        r = requests.get("https://www.adjustmenttraderalgo.tk/positions")
        total = 0
        count = 0
        for item in r.json().get('net'):
          if list(item.items())[3][1] == 'MIS':
            symbol = list(item.items())[0][1]
            quto = Quotes.getQuote(symbol,True)
            avgPrice = list(item.items())[7][1]
            logging.error('avgPrice....'+str(avgPrice))
            if avgPrice != 0:
              count =  count + 1
              avg = {symbol : avgPrice }
              dict.update(avg)
              lastprice = {symbol : quto.lastTradedPrice }
              lastPriceDict.update(lastprice)
            total = total + (dict.get(symbol) - lastPriceDict.get(symbol))
        if count == 1:
          logging.error('count----error----'+str(count))
          continue
        if total > previoustotal:
          previoustotal = total
        if total > 0 and total >= previoustotal:
          stopLoss = -25 + (total + 2)
        logging.error('total----error----'+str(total))
        logging.error('stopLoss----error----'+str(stopLoss))
        if stopLoss > total or total > 25:
          for tr in TradeManager.trades:
            logging.error('TradeManager: MTM Loss reached SL..')            
            if tr.tradeState == TradeState.ACTIVE and tr.direction == Direction.SHORT:
              telegram_send.send(messages=["25 POINTS - STOP LOSS TRIGGER ALERT !"])            
              tr.tradeState = TradeState.DISABLED
              existTrade = copy(tr)
              existTrade.tradeID = Utils.generateTradeID()
              existTrade.tradeState = TradeState.CREATED
              existTrade.direction = Direction.LONG
              TradeManager.trades.append(existTrade)
        for tr in TradeManager.trades:
          if tr.intradaySquareOffTimestamp != None:
           nowEpoch = Utils.getEpoch()
          if nowEpoch >= tr.intradaySquareOffTimestamp and tr.tradeState == TradeState.ACTIVE and tr.direction == Direction.SHORT:
            logging.error('TradeManager: intradaySquareOffTimestamp reached closing..')
            telegram_send.send(messages=["INTRADAY CLOSING TIME REACHED - ALERT !"])            
            tr.tradeState = TradeState.DISABLED 
            existTradeTimeout = copy(tr)
            existTradeTimeout.tradeID = Utils.generateTradeID()
            existTradeTimeout.tradeState = TradeState.CREATED
            existTradeTimeout.direction = Direction.LONG
            TradeManager.trades.append(existTradeTimeout)
        # TradeManager.fetchAndUpdateAllTradeOrders()
        # # track each trade and take necessary action
        # TradeManager.trackAndUpdateAllTrades()
      except Exception as e:
        logging.exception("Exception in TradeManager Main thread SL will not work")
      # save updated data to json file
      TradeManager.saveAllTradesToFile()
      # sleep for 5 seconds and then continue
      time.sleep(5)
      logging.error('TradeManager: Main thread woke up..')

  @staticmethod
  def registerStrategy(strategyInstance):
    TradeManager.strategyToInstanceMap[strategyInstance.getName()] = strategyInstance

  @staticmethod
  def loadAllTradesFromFile():
    tradesFilepath = os.path.join(TradeManager.intradayTradesDir, 'trades.json')
    if os.path.exists(tradesFilepath) == False:
      logging.warn('TradeManager: loadAllTradesFromFile() Trades Filepath %s does not exist', tradesFilepath)
      return
    TradeManager.trades = []
    tFile = open(tradesFilepath, 'r')
    tradesData = json.loads(tFile.read())
    for tr in tradesData:
      trade = TradeManager.convertJSONToTrade(tr)
      logging.info('loadAllTradesFromFile trade => %s', trade)
      TradeManager.trades.append(trade)
      if trade.tradingSymbol not in TradeManager.registeredSymbols:
        # Algo register symbols with ticker
        TradeManager.ticker.registerSymbols([trade.tradingSymbol])
        TradeManager.registeredSymbols.append(trade.tradingSymbol)
    logging.info('TradeManager: Successfully loaded %d trades from json file %s', len(TradeManager.trades), tradesFilepath)

  @staticmethod
  def saveAllTradesToFile():
    tradesFilepath = os.path.join(TradeManager.intradayTradesDir, 'trades.json')
    with open(tradesFilepath, 'w') as tFile:
      json.dump(TradeManager.trades, tFile, indent=2, cls=TradeEncoder)
    logging.info('TradeManager: Saved %d trades to file %s', len(TradeManager.trades), tradesFilepath)

  @staticmethod
  def addNewTrade(trade):
    if trade == None:
      return
    logging.error('TradeManager: addNewTrade called for %s', trade) 
    for tr in TradeManager.trades:
      if tr.equals(trade):
        logging.error('TradeManager: Trade already exists so not adding again. %s', trade)
        # if tr.tradeState == TradeState.DISABLED:
        #   continue
        return
    # Add the new trade to the list
    
    TradeManager.trades.append(trade)
    logging.error('TradeManager: trade %s added successfully to the list', trade.tradeID)
    # Register the symbol with ticker so that we will start getting ticks for this symbol

    if trade.tradingSymbol not in TradeManager.registeredSymbols:
      TradeManager.ticker.registerSymbols([Utils.prepareMonthlyExpiryFuturesSymbol('NIFTY')])
      #TradeManager.ticker.registerSymbols([trade.tradingSymbol])
      TradeManager.registeredSymbols.append(trade.tradingSymbol)
      TradeManager.directionDict = {trade.tradingSymbol:trade.direction}
    # elif str(TradeManager.directionDict.get(trade.tradingSymbol)) == trade.direction:
    #   TradeManager.ticker.registerSymbols([trade.tradingSymbol])
    #   TradeManager.registeredSymbols.append(trade.tradingSymbol)
    # Also add the trade to strategy trades list
    strategyInstance = TradeManager.strategyToInstanceMap[trade.strategy]
    if strategyInstance != None:
      strategyInstance.addTradeToList(trade)

  @staticmethod
  def disableTrade(trade, reason):
    if trade != None:
      logging.info('TradeManager: Going to disable trade ID %s with the reason %s', trade.tradeID, reason)
      trade.tradeState = TradeState.DISABLED

  @staticmethod
  def tickerListener(tick):
    # while True:
    #   tick.lastTradedPrice += 5
    #   time.sleep(1)
    #   if tick.lastTradedPrice == 17455.5:
    #     tick.lastTradedPrice = 17338
    #   if tick.lastTradedPrice == 17378:
    #     tick.lastTradedPrice = 17320
      # logging.info('tickerLister: new tick received for %s = %f', tick.tradingSymbol, tick.lastTradedPrice);
      TradeManager.symbolToCMPMap[tick.tradingSymbol] = tick.lastTradedPrice # Store the latest tick in map
      # On each new tick, get a created trade and call its strategy whether to place trade or not
      for strategy in TradeManager.strategyToInstanceMap:
        strategyInstance = TradeManager.strategyToInstanceMap[strategy]
        if tick.tradingSymbol == Utils.prepareMonthlyExpiryFuturesSymbol('NIFTY'):
          result = strategyInstance.adjustment(str(tick.lastTradedPrice))
        longTrade = TradeManager.getUntriggeredTrade(tick.tradingSymbol, strategy, Direction.LONG)
        shortTrade = TradeManager.getUntriggeredTrade(tick.tradingSymbol, strategy, Direction.SHORT)
        if longTrade == None and shortTrade == None:
          continue    
        if longTrade != None:     
          if strategyInstance.shouldPlaceTrade(longTrade, tick):
            # place the longTrade
            isSuccess = TradeManager.executeTrade(longTrade)
            if isSuccess == True:
              # set longTrade state to ACTIVE
              longTrade.tradeState = TradeState.DISABLED
              for tr in TradeManager.trades:
                if tr.tradingSymbol == longTrade.tradingSymbol and tr.tradeState == TradeState.ACTIVE and tr.direction == Direction.SHORT:     
                  tr.tradeState = TradeState.DISABLED
              longTrade.startTimestamp = Utils.getEpoch()
              continue
        if shortTrade != None:     
          if strategyInstance.shouldPlaceTrade(shortTrade, tick):
            # place the shortTrade
            isSuccess = TradeManager.executeTrade(shortTrade)
            if isSuccess == True:
              # set shortTrade state to ACTIVE
              shortTrade.tradeState = TradeState.ACTIVE
              shortTrade.startTimestamp = Utils.getEpoch()

  @staticmethod
  def getUntriggeredTrade(tradingSymbol, strategy, direction):
    trade = None
    for tr in TradeManager.trades:     
      if tr.tradeState == TradeState.DISABLED:
        continue
      if tr.tradeState != TradeState.CREATED:
        continue
      if tr.tradingSymbol != tradingSymbol and tr.direction != direction:
        continue
      if tr.strategy != strategy:
        continue
      if tr.direction != direction:
        continue
      trade = tr
      break
    return trade

  @staticmethod
  def executeTrade(trade):
    logging.error('TradeManager: Execute trade called for %s', trade)
    trade.initialStopLoss = trade.stopLoss
    # Create order input params object and place order
    oip = OrderInputParams(trade.tradingSymbol)
    oip.direction = trade.direction
    oip.productType = trade.productType
    oip.orderType = OrderType.MARKET if trade.placeMarketOrder == True else OrderType.LIMIT
    oip.price = trade.requestedEntry
    oip.qty = trade.qty
    if trade.isFutures == True or trade.isOptions == True:
      oip.isFnO = True
    logging.error("ORDERS EXECUTED--"+str(oip))
    try:
      trade.entryOrder = TradeManager.getOrderManager().placeOrder(oip)
    except Exception as e:
      logging.error('TradeManager: Execute trade failed for tradeID %s: Error => %s', trade.tradeID, str(e))
      telegram_send.send(messages=["ALERT HIGH HIGH"])
      telegram_send.send(messages=[str(e)])
      return False
    logging.error('TradeManager: Execute trade successful for %s and entryOrder %s', trade, trade.entryOrder)
    return True

  @staticmethod
  def fetchAndUpdateAllTradeOrders():
    allOrders = []
    for trade in TradeManager.trades:
      if trade.entryOrder != None:
        allOrders.append(trade.entryOrder)
      if trade.slOrder != None:
        allOrders.append(trade.slOrder)
      if trade.targetOrder != None:
        allOrders.append(trade.targetOrder)

    TradeManager.getOrderManager().fetchAndUpdateAllOrderDetails(allOrders)

  @staticmethod
  def trackAndUpdateAllTrades():
    for trade in TradeManager.trades:
      if trade.tradeState == TradeState.ACTIVE or  trade.tradeState == TradeState.DISABLED:
        TradeManager.trackEntryOrder(trade)
        #TradeManager.trackSLOrder(trade)
        #TradeManager.trackTargetOrder(trade)
        if trade.intradaySquareOffTimestamp != None:
          nowEpoch = Utils.getEpoch()
          if nowEpoch >= trade.intradaySquareOffTimestamp:
            TradeManager.squareOffTrade(trade, TradeExitReason.SQUARE_OFF)

  @staticmethod
  def trackEntryOrder(trade):
    if trade.tradeState != TradeState.ACTIVE:
      return

    if trade.entryOrder == None:
      return

    if trade.entryOrder.orderStatus == OrderStatus.CANCELLED or trade.entryOrder.orderStatus == OrderStatus.REJECTED:
      trade.tradeState = TradeState.CANCELLED

    trade.filledQty = trade.entryOrder.filledQty
    if trade.filledQty > 0:
      trade.entry = trade.entryOrder.averagePrice
    # Update the current market price and calculate pnl
    trade.cmp = TradeManager.symbolToCMPMap[trade.tradingSymbol]
    Utils.calculateTradePnl(trade)

  @staticmethod
  def trackSLOrder(trade):
    if trade.tradeState != TradeState.ACTIVE:
      return
    if trade.stopLoss == 0: # Do not place SL order if no stopLoss provided
      return
    if trade.slOrder == None:
      # Place SL order
      TradeManager.placeSLOrder(trade)
    else:
      if trade.slOrder.orderStatus == OrderStatus.COMPLETE:
        # SL Hit
        exit = trade.slOrder.averagePrice
        exitReason = TradeExitReason.SL_HIT if trade.initialStopLoss == trade.stopLoss else TradeExitReason.TRAIL_SL_HIT
        TradeManager.setTradeToCompleted(trade, exit, exitReason)
        # Make sure to cancel target order if exists
        TradeManager.cancelTargetOrder(trade)

      elif trade.slOrder.orderStatus == OrderStatus.CANCELLED:
        # SL order cancelled outside of algo (manually or by broker or by exchange)
        logging.error('SL order %s for tradeID %s cancelled outside of Algo. Setting the trade as completed with exit price as current market price.', trade.slOrder.orderId, trade.tradeID)
        exit = TradeManager.symbolToCMPMap[trade.tradingSymbol]
        TradeManager.setTradeToCompleted(trade, exit, TradeExitReason.SL_CANCELLED)
        # Cancel target order if exists
        TradeManager.cancelTargetOrder(trade)

      else:
        TradeManager.checkAndUpdateTrailSL(trade)

  @staticmethod
  def checkAndUpdateTrailSL(trade):
    # Trail the SL if applicable for the trade
    strategyInstance = TradeManager.strategyToInstanceMap[trade.strategy]
    if strategyInstance == None:
      return

    newTrailSL = strategyInstance.getTrailingSL(trade)
    updateSL = False
    if newTrailSL > 0:
      if trade.direction == Direction.LONG and newTrailSL > trade.stopLoss:
        updateSL = True
      elif trade.direction == Direction.SHORT and newTrailSL < trade.stopLoss:
        updateSL = True
    if updateSL == True:
      omp = OrderModifyParams()
      omp.newTriggerPrice = newTrailSL
      try:
        oldSL = trade.stopLoss
        TradeManager.getOrderManager().modifyOrder(trade.slOrder, omp)
        logging.info('TradeManager: Trail SL: Successfully modified stopLoss from %f to %f for tradeID %s', oldSL, newTrailSL, trade.tradeID)
        trade.stopLoss = newTrailSL # IMPORTANT: Dont forget to update this on successful modification
      except Exception as e:
        logging.error('TradeManager: Failed to modify SL order for tradeID %s orderId %s: Error => %s', trade.tradeID, trade.slOrder.orderId, str(e))

  @staticmethod
  def trackTargetOrder(trade):
    if trade.tradeState != TradeState.ACTIVE:
      return
    if trade.target == 0: # Do not place Target order if no target provided
      return
    if trade.targetOrder == None:
      # Place Target order
      TradeManager.placeTargetOrder(trade)
    else:
      if trade.targetOrder.orderStatus == OrderStatus.COMPLETE:
        # Target Hit
        exit = trade.targetOrder.averagePrice
        TradeManager.setTradeToCompleted(trade, exit, TradeExitReason.TARGET_HIT)
        # Make sure to cancel sl order
        TradeManager.cancelSLOrder(trade)

      elif trade.targetOrder.orderStatus == OrderStatus.CANCELLED:
        # Target order cancelled outside of algo (manually or by broker or by exchange)
        logging.error('Target order %s for tradeID %s cancelled outside of Algo. Setting the trade as completed with exit price as current market price.', trade.targetOrder.orderId, trade.tradeID)
        exit = TradeManager.symbolToCMPMap[trade.tradingSymbol]
        TradeManager.setTradeToCompleted(trade, exit, TradeExitReason.TARGET_CANCELLED)
        # Cancel SL order
        TradeManager.cancelSLOrder(trade)

  @staticmethod
  def placeSLOrder(trade):
    oip = OrderInputParams(trade.tradingSymbol)
    oip.direction = Direction.SHORT if trade.direction == Direction.LONG else Direction.LONG 
    oip.productType = trade.productType
    oip.orderType = OrderType.SL_MARKET
    oip.triggerPrice = trade.stopLoss
    oip.qty = trade.qty
    if trade.isFutures == True or trade.isOptions == True:
      oip.isFnO = True
    try:
      trade.slOrder = TradeManager.getOrderManager().placeOrder(oip)
    except Exception as e:
      logging.error('TradeManager: Failed to place SL order for tradeID %s: Error => %s', trade.tradeID, str(e))
      return False
    logging.info('TradeManager: Successfully placed SL order %s for tradeID %s', trade.slOrder.orderId, trade.tradeID)
    return True

  @staticmethod
  def placeTargetOrder(trade, isMarketOrder = False):
    oip = OrderInputParams(trade.tradingSymbol)
    oip.direction = Direction.SHORT if trade.direction == Direction.LONG else Direction.LONG
    oip.productType = trade.productType
    oip.orderType = OrderType.MARKET if isMarketOrder == True else OrderType.LIMIT
    oip.price = 0 if isMarketOrder == True else trade.target
    oip.qty = trade.qty
    if trade.isFutures == True or trade.isOptions == True:
      oip.isFnO = True
    try:
      trade.targetOrder = TradeManager.getOrderManager().placeOrder(oip)
    except Exception as e:
      logging.error('TradeManager: Failed to place Target order for tradeID %s: Error => %s', trade.tradeID, str(e))
      return False
    logging.info('TradeManager: Successfully placed Target order %s for tradeID %s', trade.targetOrder.orderId, trade.tradeID)
    return True

  @staticmethod
  def cancelEntryOrder(trade):
    if trade.entryOrder == None:
      return
    if trade.entryOrder.orderStatus == OrderStatus.CANCELLED:
      return
    try:
      TradeManager.getOrderManager().cancelOrder(trade.entryOrder)
    except Exception as e:
      logging.error('TradeManager: Failed to cancel Entry order %s for tradeID %s: Error => %s', trade.entryOrder.orderId, trade.tradeID, str(e))
    logging.info('TradeManager: Successfully cancelled Entry order %s for tradeID %s', trade.entryOrder.orderId, trade.tradeID)

  @staticmethod
  def cancelSLOrder(trade):
    if trade.slOrder == None:
      return
    if trade.slOrder.orderStatus == OrderStatus.CANCELLED:
      return
    try:
      TradeManager.getOrderManager().cancelOrder(trade.slOrder)
    except Exception as e:
      logging.error('TradeManager: Failed to cancel SL order %s for tradeID %s: Error => %s', trade.slOrder.orderId, trade.tradeID, str(e))
    logging.info('TradeManager: Successfully cancelled SL order %s for tradeID %s', trade.slOrder.orderId, trade.tradeID)

  @staticmethod
  def cancelTargetOrder(trade):
    if trade.targetOrder == None:
      return
    if trade.targetOrder.orderStatus == OrderStatus.CANCELLED:
      return
    try:
      TradeManager.getOrderManager().cancelOrder(trade.targetOrder)
    except Exception as e:
      logging.error('TradeManager: Failed to cancel Target order %s for tradeID %s: Error => %s', trade.targetOrder.orderId, trade.tradeID, str(e))
    logging.info('TradeManager: Successfully cancelled Target order %s for tradeID %s', trade.targetOrder.orderId, trade.tradeID)

  @staticmethod
  def setTradeToCompleted(trade, exit, exitReason = None):
    trade.tradeState = TradeState.COMPLETED
    trade.exit = exit
    trade.exitReason = exitReason if trade.exitReason == None else trade.exitReason
    trade.endTimestamp = Utils.getEpoch()
    trade = Utils.calculateTradePnl(trade)
    logging.info('TradeManager: setTradeToCompleted strategy = %s, symbol = %s, qty = %d, entry = %f, exit = %f, pnl = %f, exit reason = %s', trade.strategy, trade.tradingSymbol, trade.filledQty, trade.entry, trade.exit, trade.pnl, trade.exitReason)

  @staticmethod
  def squareOffTrade(trade, reason = TradeExitReason.SQUARE_OFF):
    logging.info('TradeManager: squareOffTrade called for tradeID %s with reason %s', trade.tradeID, reason)
    if trade == None or trade.tradeState != TradeState.ACTIVE:
      return

    trade.exitReason = reason
    if trade.entryOrder != None:
      if trade.entryOrder.orderStatus == OrderStatus.OPEN:
        # Cancel entry order if it is still open (not filled or partially filled case)
        TradeManager.cancelEntryOrder(trade)

    if trade.slOrder != None:
      TradeManager.cancelSLOrder(trade)

    if trade.targetOrder != None:
      # Change target order type to MARKET to exit position immediately
      logging.info('TradeManager: changing target order %s to MARKET to exit position for tradeID %s', trade.targetOrder.orderId, trade.tradeID)
      TradeManager.getOrderManager().modifyOrderToMarket(trade.targetOrder)
    else:
      # Place new target order to exit position
      logging.info('TradeManager: placing new target order to exit position for tradeID %s', trade.tradeID)
      TradeManager.placeTargetOrder(trade, True)

  @staticmethod
  def getOrderManager():
    orderManager = None
    brokerName = Controller.getBrokerName()
    if brokerName == "zerodha":
      orderManager = ZerodhaOrderManager()
    #elif brokerName == "fyers": # Not implemented
    return orderManager

  @staticmethod
  def getNumberOfTradesPlacedByStrategy(strategy):
    count = 0
    for trade in TradeManager.trades:
      if trade.strategy != strategy:
        continue
      if trade.tradeState == TradeState.CREATED or trade.tradeState == TradeState.DISABLED:
        continue
      # consider active/completed/cancelled trades as trades placed
      count += 1
    return count

  @staticmethod
  def getAllTradesByStrategy(strategy):
    tradesByStrategy = []
    for trade in TradeManager.trades:
      if trade.strategy == strategy:
        tradesByStrategy.append(trade)
    return tradesByStrategy

  @staticmethod
  def convertJSONToTrade(jsonData):
    trade = Trade(jsonData['tradingSymbol'])
    trade.tradeID = jsonData['tradeID']
    trade.strategy = jsonData['strategy']
    trade.direction = jsonData['direction']
    trade.productType = jsonData['productType']
    trade.isFutures = jsonData['isFutures']
    trade.isOptions = jsonData['isOptions']
    trade.optionType = jsonData['optionType']
    trade.placeMarketOrder = jsonData['placeMarketOrder']
    trade.intradaySquareOffTimestamp = jsonData['intradaySquareOffTimestamp']
    trade.requestedEntry = jsonData['requestedEntry']
    trade.entry = jsonData['entry']
    trade.qty = jsonData['qty']
    trade.filledQty = jsonData['filledQty']
    trade.initialStopLoss = jsonData['initialStopLoss']
    trade.stopLoss = jsonData['stopLoss']
    trade.target = jsonData['target']
    trade.cmp = jsonData['cmp']
    trade.tradeState = jsonData['tradeState']
    trade.timestamp = jsonData['timestamp']
    trade.createTimestamp = jsonData['createTimestamp']
    trade.startTimestamp = jsonData['startTimestamp']
    trade.endTimestamp = jsonData['endTimestamp']
    trade.pnl = jsonData['pnl']
    trade.pnlPercentage = jsonData['pnlPercentage']
    trade.exit = jsonData['exit']
    trade.exitReason = jsonData['exitReason']
    trade.exchange = jsonData['exchange']
    trade.entryOrder = TradeManager.convertJSONToOrder(jsonData['entryOrder'])
    trade.slOrder = TradeManager.convertJSONToOrder(jsonData['slOrder'])
    trade.targetOrder = TradeManager.convertJSONToOrder(jsonData['targetOrder'])
    return trade

  @staticmethod
  def convertJSONToOrder(jsonData):
    if jsonData == None:
      return None
    order = Order()
    order.tradingSymbol = jsonData['tradingSymbol']
    order.exchange = jsonData['exchange']
    order.productType = jsonData['productType']
    order.orderType = jsonData['orderType']
    order.price = jsonData['price']
    order.triggerPrice = jsonData['triggerPrice']
    order.qty = jsonData['qty']
    order.orderId = jsonData['orderId']
    order.orderStatus = jsonData['orderStatus']
    order.averagePrice = jsonData['averagePrice']
    order.filledQty = jsonData['filledQty']
    order.pendingQty = jsonData['pendingQty']
    order.orderPlaceTimestamp = jsonData['orderPlaceTimestamp']
    order.lastOrderUpdateTimestamp = jsonData['lastOrderUpdateTimestamp']
    order.message = jsonData['message']
    return order

  @staticmethod
  def getLastTradedPrice(tradingSymbol):
    return TradeManager.symbolToCMPMap[tradingSymbol]
