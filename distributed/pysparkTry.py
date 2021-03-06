#-*- coding:utf-8 -*-#
import socket
import logging
import numpy as np
import tensorflow as tf
import tensorflow.contrib.slim as slim
import pyspark
import requests
import time
import random
import multiprocessing
import datetime
import math
from pyspark import SparkContext, SparkConf
from pyspark.sql import SparkSession, Row
from pyspark.sql.types import LongType
from pyspark.ml.classification import DecisionTreeClassifier, DecisionTreeClassificationModel
from util.data import getMnist, getDatasetMinist
from spark_sklearn.util import createLocalSparkSession
from sklearn.preprocessing import LabelEncoder



def getLogger():

    logging.basicConfig(level=logging.INFO,
                        format='%(asctime)s %(filename)s[line:%(lineno)d] %(levelname)s %(message)s',
                        # filename='/home/gyy/pysparkTry.log',
                        # filemode='w',
                        datefmt='%a, %d %b %Y %H:%M:%S',
                        )
    return logging.getLogger(__name__)


def isPortAvaliable(host, port):
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    res = sock.connect_ex((host, port))
    if res == 0:
        sock.close()
        return False
    return True

def createPort():
    """

    :return:
    """
    basePort = random.randint(3000, 4000)
    while not isPortAvaliable("localhost", basePort):
        print("port %s is not available" % basePort)
    return basePort

def getClusterAndServer():
    """
        The spec is fix for this demo,
        It's need to design a spec provider
    :param jobName:
    :param taskIndex:
    :return:
    """

    # ports = []
    # for _ in range(4):
    #     port = createPort()
    #     if port not in ports:
    #         ports.append(port)
    # print("prots ars %s"%str(ports))
    ports = ["3222","3223","3224","3225"]

    def f(jobName, taskIndex):
        cluster_spec = {
            "worker": [
                "localhost:%s" % ports[0],
                "localhost:%s" % ports[1],
            ],
            "ps": [
                "localhost:%s" % ports[2],
                "localhost:%s" % ports[3]
            ]}
        # Create a cluster from the parameter server and worker hosts.
        cluster = tf.train.ClusterSpec(cluster_spec)

        # Create and start a server for the local task.
        server = tf.train.Server(cluster, jobName, taskIndex)

        return cluster, server
    return f


def lrmodel(x, label):
    """
        a simple full connected feedforward function model
    :param x:
    :param label:
    :return:
    """
    IMAGE_PIXELS = 20
    hidden_units = 200
    # Variables of the hidden layer
    # hid_w = tf.Variable(tf.truncated_normal([IMAGE_PIXELS * IMAGE_PIXELS, hidden_units],
    #                                         stddev=1.0 / IMAGE_PIXELS), name="hid_w")

    hid_w = tf.get_variable("hid_w", shape=[IMAGE_PIXELS * IMAGE_PIXELS, hidden_units],
                            initializer=tf.truncated_normal_initializer)

    hid_b = tf.Variable(tf.zeros([hidden_units]), name="hid_b")
    tf.summary.histogram("hidden_weights", hid_w)

    # Variables of the softmax layer
    sm_w = tf.Variable(tf.truncated_normal([hidden_units, 10],
                                           stddev=1.0 / math.sqrt(hidden_units)), name="sm_w")
    sm_b = tf.Variable(tf.zeros([10]), name="sm_b")

    hid_lin = tf.nn.xw_plus_b(x, hid_w, hid_b, name="hid_lin")
    hid = tf.nn.relu(hid_lin, name="hid")


    logits = tf.nn.xw_plus_b(hid, sm_w, sm_b, name="logits")
    pred = tf.nn.softmax(logits, name="pred")

    # loss = -tf.reduce_sum(label * tf.log(tf.clip_by_value(pred, 1e-10, 1.0)), name="loss")
    loss = tf.reduce_mean(tf.nn.softmax_cross_entropy_with_logits(labels=label, logits=logits), name="loss")

    globalStep = tf.contrib.framework.get_or_create_global_step()
    opt = tf.train.AdamOptimizer(0.001)
    return pred, loss, globalStep, opt




def tff(model, getClusterAndServer, getlogger):
    """

    :param model:
    :param getClusterAndServer:
    :param getlogger:
    :return:
    """
    def getIndex():
        """
            use a index system to decide worker index
        :return:
        """
        time.sleep(random.random()*0.1)
        api = "http://127.0.0.1:5000/getindex"
        result = eval(requests.get(api).text)
        return int(result["index"])

    def createDoneQueue(workers, i):
        """Queue used to signal death for i'th ps shard. Intended to have
        all workers enqueue an item onto it to signal doneness."""

        with tf.device("/job:ps/task:%d" % (i)):
            return tf.FIFOQueue(workers, tf.int32, shared_name="done_queue" +
                                                                     str(i))

    def createDoneQueues(cluster):
        return [createDoneQueue(cluster.num_tasks('worker'), i) for i in range(cluster.num_tasks('ps'))]

    def runWorker(index, dataX, dataY, logger, isAsyn=True):
        jobName = 'worker'
        cluster, server = getClusterAndServer(jobName, index)
        with tf.device(tf.train.replica_device_setter(
                worker_device="/job:worker/task:%d" % index,
                cluster=cluster)):
            x = tf.placeholder(dtype=tf.float32, shape=[None, 400])
            label = tf.placeholder(dtype=tf.float32, shape=[None, 10])
            pred, loss, globalStep, opt = model(x, label)
            trainOp = opt.minimize(loss, global_step=globalStep)
            uninitedOp = tf.report_uninitialized_variables()

        queues = createDoneQueues(cluster)
        enqueueOps = [q.enqueue(1) for q in queues]


        init_op = tf.global_variables_initializer()
        sess_config = tf.ConfigProto(
            allow_soft_placement=True,
            log_device_placement=False,
            device_filters=["/job:ps", "/job:worker/task:%d" % index])

        sv = tf.train.Supervisor(is_chief=(index == 0),
                                 init_op=init_op,
                                 summary_op=None,
                                 summary_writer=None,
                                 stop_grace_secs=300,
                                 global_step=globalStep)

        # with sv.managed_session(server.target) as sess:
        with sv.prepare_or_wait_for_session(server.target,
                                            config=sess_config) as sess:

            # wait for parameter server variables to be initialized
            while (len(sess.run(uninitedOp)) > 0):
                logger.info("worker %d: ps uninitialized, sleeping" % index)
                time.sleep(1)

            logger.info("worker %d: is initialized, start working" % index)
            for i in range(100):
                _, l, step = sess.run([trainOp, loss, globalStep], feed_dict={x: dataX, label: dataY})
                logger.info("workder index %s, iter %s, loss is %s, global step is %s" % (index, i, l, step))

            while globalStep.eval() < 200-1:
                time.sleep(1e-1)

            for op in enqueueOps:
                sess.run(op)

        logger.info("{0} stopping supervisor".format(datetime.datetime.now().isoformat()))
        sv.stop()

    def runWorkerSyn(index, dataX, dataY, logger):
        """
            A synchronized distributed training method
        :param index:
        :param dataX:
        :param dataY:
        :param logger:
        :return:
        """
        jobName = 'worker'
        is_chief = (index == 0)
        cluster, server = getClusterAndServer(jobName, index)
        with tf.device(tf.train.replica_device_setter(
                worker_device="/job:worker/task:%d" % index,
                cluster=cluster)):
            x = tf.placeholder(dtype=tf.float32, shape=[None, 400])
            label = tf.placeholder(dtype=tf.float32, shape=[None, 10])
            pred, loss, globalStep, opt = model(x, label)

            opt = tf.train.SyncReplicasOptimizer(opt, replicas_to_aggregate=2,
                                                 total_num_replicas=2)
            trainOp = opt.minimize(loss, global_step=globalStep)

            uninitedOp = tf.report_uninitialized_variables()

        queues = createDoneQueues(cluster)
        enqueueOps = [q.enqueue(1) for q in queues]

        if is_chief:
            chief_queue_runner = opt.get_chief_queue_runner()
            init_tokens_op = opt.get_init_tokens_op()

        if is_chief:
            logger.info("Worker %d: Initializing session..." % index)
        else:
            logger.info("Worker %d: Waiting for session to be initialized..." %
                  index)

        sess_config = tf.ConfigProto(
            allow_soft_placement=True,
            log_device_placement=False,
            device_filters=["/job:ps", "/job:worker/task:%d" % index])

        init_op = tf.global_variables_initializer()

        sv = tf.train.Supervisor(is_chief=is_chief,
                                 init_op=init_op,
                                 summary_op=None,
                                 summary_writer=None,
                                 stop_grace_secs=300,
                                 global_step=globalStep)

        with sv.prepare_or_wait_for_session(server.target,
                                              config=sess_config) as sess:

            print("Starting chief queue runner and running init_tokens_op")
            if is_chief:
                sv.start_queue_runners(sess, [chief_queue_runner])
                sess.run(init_tokens_op)

            #wait for parameter server variables to be initialized
            while (len(sess.run(uninitedOp)) > 0):
                logger.info("worker %d: ps uninitialized, sleeping" % index)
                time.sleep(1)

            step = 0
            logger.info("worker %d: is initialized, start working" % index)
            while step < 100:
                _, l, step = sess.run([trainOp, loss, globalStep], feed_dict={x: dataX, label: dataY})

                logger.info("workder index %s, loss is %s, global_step is %s" % (index, l, step))

            for op in enqueueOps:
                sess.run(op)

        logger.info("{0} stopping supervisor".format(datetime.datetime.now().isoformat()))

    def runPs(index, logger):
        jobName = 'ps'
        cluster, server = getClusterAndServer(jobName, index)

        logger.info("start server %s" % index)

        queue = createDoneQueue(cluster.num_tasks('worker'), index)

        with tf.Session(server.target) as sess:
            for i in range(cluster.num_tasks('worker')):
                sess.run(queue.dequeue())
        logger.info("end server %s" % index)

    def runPsSyn(index, logger):
        jobName = 'ps'
        cluster, server = getClusterAndServer(jobName, index)

        logger.info("start server %s" % index)
        server.join()
        logger.info("end server %s" % index)

    def fSyn(iter):
        logger = getlogger()
        data = []
        for item in iter:
            data.append(item)

        data = np.vstack(data)
        logger.info("data shape is %s %s" % (data.shape[0], data.shape[1]))

        dataX = data[:,:400]
        dataY = data[:,400:]
        index = getIndex()

        p = multiprocessing.Process(target=runPsSyn, args=(index, logger,))
        p.start()

        runWorkerSyn(index, dataX, dataY, logger)

    def f(iter):
        logger = getlogger()
        data = []
        for item in iter:
            data.append(item)

        data = np.vstack(data)
        logger.info("data shape is %s %s" % (data.shape[0], data.shape[1]))

        dataX = data[:,:400]
        dataY = data[:,400:]
        index = getIndex()

        p = multiprocessing.Process(target=runPs, args=(index, logger,))
        p.start()

        runWorker(index, dataX, dataY, logger)

    return f


def main():
    """
        a simple pyspark + tensorflow + mnist for local distributed test

        Use a index restful api to provide different index, and all executor both
        initialize ps and worker.

        Use figoqueue to control parameter server.
        After ps initialize, they will run an dequeue operation, which will block the process until element is added
        After each worker end its work, they will enqueue 1 to queue. And the dequeue operation of ps will be exeucuted.
        Ps will be released, after all work of each worker are done.
    :return:
    """
    # TODO: 1. Cluster spec provider. 2. How to decide whether executor should init ps or not
    # spark = SparkSession.builder.appName("test").master("local").getOrCreate()
    # conf = SparkConf().setAppName("test").setMaster("spark://namenode01:7077") \
    #     .set("spark.shuffle.service.enabled", "false").set("spark.dynamicAllocation.enabled", "false")
        #.set('spark.executor.instances', '1').set('spark.executor.cores', '1')\
        #.set('spark.executor.memory', '512m').set('spark.driver.memory', '512m')\
    conf = SparkConf().setAppName("test").setMaster("local[2]") \
        .set("spark.shuffle.service.enabled", "false").set("spark.dynamicAllocation.enabled", "false")

    sc = SparkContext(conf=conf)
    dataX, dataY = getMnist()
    data = np.split(np.hstack([dataX, dataY]), dataX.shape[0], axis=0)

    rdd = sc.parallelize(data, 2)
    rdd.foreachPartition(tff(lrmodel, getClusterAndServer, getLogger))
    print("_________ end _________")


def dfTry():
    spark = createLocalSparkSession()
    df = getDatasetMinist(spark)

    rdd1 = spark.sparkContext.parallelize(np.arange(5000).tolist())

    rdd2 = df.rdd.zip(rdd1).map(lambda d_r: d_r[0]+Row(pred=d_r[1]))

    df2 = df.sql_ctx.createDataFrame(rdd2, df.schema.add("pred", LongType()))
    df2.show()


def gbtr():
    spark = createLocalSparkSession()
    df = getDatasetMinist(spark)
    # DecisionTreeClassifier.le = LabelEncoder()
    clf = DecisionTreeClassifier()
    model = clf.fit(df)
    model.write().overwrite().save('file:///data/mapleleaf/work/algorithm/model/DecisionTreeClassifier_node_DecisionTreeClassifier_76017b.None/model')

def load():
    spark = createLocalSparkSession()
    obj = DecisionTreeClassificationModel.load('tmp')
    # print(obj.le)


    # unzip_file(zipf, '/tmp/model/', True)

def read_zip():
    zipf = '/tmp/model/tmp.zip'
    with open(zipf, 'rb') as f:
        b_text = f.read()

    with open('/tmp/model/tmp2.zip', 'wb') as w:
        w.write(b_text)

if __name__ == '__main__':
    pass
    # cluster, server = getClusterAndServer("worker", 1)
    # print(cluster.num_tasks("worker"))