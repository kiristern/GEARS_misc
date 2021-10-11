from copy import deepcopy
import argparse
from time import time
import sys
sys.path.append('/dfs/user/yhr/cell_reprogram/model/')

import scanpy as sc
import numpy as np

import torch
import torch.optim as optim
import torch.nn as nn
from torch.optim.lr_scheduler import StepLR

from model import linear_model, simple_GNN, simple_GNN_AE, GNN_Disentangle, AE
from data import PertDataloader, Network
from inference import evaluate, compute_metrics
from utils import loss_fct

torch.manual_seed(0)


def train(model, train_loader, val_loader, graph, weights, args, device="cpu", gene_idx=None):
    if args['wandb']:
        import wandb
    optimizer = optim.Adam(model.parameters(), lr=args['lr'], weight_decay=args['weight_decay'])
    scheduler = StepLR(optimizer, step_size=args['lr_decay_step_size'], gamma=args['lr_decay_factor'])

    min_val = np.inf
    
    print('Start Training...')
    
    for epoch in range(args["max_epochs"]):
        total_loss = 0
        model.train()
        num_graphs = 0

        for step, batch in enumerate(train_loader):

            batch.to(device)
            graph = graph.to(device)
            if weights is not None:
                weights = weights.to(device)
            
            model.to(device)
            optimizer.zero_grad()
            pred = model(batch, graph, weights)
            y = batch.y

            # Compute loss
            loss = loss_fct(pred, y, batch.pert, args['pert_loss_wt'], 
                              loss_mode = args['loss_mode'], 
                              gamma = args['focal_gamma'],
                              loss_type = args['loss_type'])
            loss.backward()
            nn.utils.clip_grad_value_(model.parameters(), clip_value=1.0)
            
            optimizer.step()
            total_loss += loss.item() * batch.num_graphs
            num_graphs += batch.num_graphs
            
            if args['wandb']:
                wandb.log({'training_loss': loss.item()})
                
            if step % args["print_progress_steps"] == 0:
                log = "Epoch {} Step {} Train Loss: {:.4f}" 
                print(log.format(epoch + 1, step + 1, loss.item()))
        scheduler.step()
        # Evaluate model performance on train and val set
        total_loss /= num_graphs
        train_res = evaluate(train_loader, graph, weights, model, args, gene_idx=gene_idx)
        val_res = evaluate(val_loader, graph, weights, model, args, gene_idx=gene_idx)
        train_metrics, _ = compute_metrics(train_res, gene_idx=gene_idx)
        val_metrics, _ = compute_metrics(val_res, gene_idx=gene_idx)
        
        # Print epoch performance
        log = "Epoch {}: Train: {:.4f}, R2 {:.4f} " \
              "Validation: {:.4f}. R2 {:.4f} " \
              "Loss: {:.4f}"
        print(log.format(epoch + 1, train_metrics['mse'], train_metrics['r2'],
                         val_metrics['mse'], val_metrics['r2'],
                         total_loss))
        
        if args['wandb']:
            wandb.log({'train_mse': train_metrics['mse'],
                     'train_r2': train_metrics['r2'],
                     'val_mse': val_metrics['mse'],
                     'val_r2': val_metrics['r2']})
        
        
        # Print epoch performance for DE genes
        log = "DE_Train: {:.4f}, R2 {:.4f} " \
              "DE_Validation: {:.4f}. R2 {:.4f} "
        print(log.format(train_metrics['mse_de'], train_metrics['r2_de'],
                         val_metrics['mse_de'], val_metrics['r2_de']))

        if args['wandb']:
            wandb.log({'train_de_mse': train_metrics['mse_de'],
                     'train_de_r2': train_metrics['r2_de'],
                     'val_de_mse': val_metrics['mse_de'],
                     'val_de_r2': val_metrics['r2_de']})
            
            
        # Select best model
        if val_metrics['mse'] < min_val:
            min_val = val_metrics['mse']
            best_model = deepcopy(model)

    return best_model


def trainer(args):
    print('---- Printing Arguments ----')
    for i, j in args.items():
        print(i + ': ' + str(j))
    print('----------------------------')
        
    ## exp name setup
    exp_name = args['model_backend'] + '_' + args['network_name'] + '_' + str(args['node_hidden_size']) + '_' + str(args['gnn_num_layers']) + '_' + args['loss_mode'] + '_' + args['dataset']
    
    if args['loss_mode'] == 'l3':
        exp_name += '_gamma' + str(args['focal_gamma'])

    if args['shared_weights']:
        exp_name += '_shared'
    
    args['model_name'] = exp_name
    
    if args['wandb']:
        import wandb        
        wandb.init(project=args['project_name'] + '_' + args['split'], entity=args['entity_name'], name=exp_name)
        wandb.config.update(args)
        
    if args['network_name'] == 'string':
        args['network_path'] = '/dfs/project/perturb-gnn/graphs/STRING_full_9606.csv'
    
    if args['dataset'] == 'Norman2019':
        data_path = '/dfs/project/perturb-gnn/datasets/Norman2019_hvg+perts.h5ad'
    
    s = time()
    adata = sc.read_h5ad(data_path)
    if 'gene_symbols' not in adata.var.columns.values:
        adata.var['gene_symbols'] = adata.var['gene_name']
    gene_list = [f for f in adata.var.gene_symbols.values]
    args['gene_list'] = gene_list
    args['num_genes'] = len(gene_list)
    
    try:
        args['num_ctrl_samples'] = adata.uns['num_ctrl_samples']
    except:
        args['num_ctrl_samples'] = 1

    print('Training '+ args['model_name'])
    print('Building cell graph... ')

    # Set up message passing network
    network = Network(fname=args['network_path'], gene_list=args['gene_list'],
                      percentile=args['top_edge_percent'])

    # Pertrubation dataloader
    pertdl = PertDataloader(adata, network.G, network.weights, args)

    # Compute number of features for each node
    item = [item for item in pertdl.loaders['train_loader']][0]
    args['num_node_features'] = item.x.shape[1]
    print('Finished data setup, in total takes ' + str((time() - s)/60)[:5] + ' min')
    
    print('Initializing model... ')
    
    # Train a model
    # Define model
    if args['model'] == 'GNN_simple':
        model = simple_GNN(args['num_node_features'],
                           args['num_genes'],
                           args['node_hidden_size'],
                           args['node_embed_size'],
                           args['edge_weights'],
                           args['loss_type'])

    elif args['model'] == 'GNN_AE':
        model = simple_GNN_AE(args['num_node_features'],
                           args['num_genes'],
                           args['node_hidden_size'],
                           args['node_embed_size'],
                           args['edge_weights'],
                           args['ae_num_layers'],
                           args['ae_hidden_size'],
                           args['loss_type'])
    elif args['model'] == 'GNN_Disentangle':
        model = GNN_Disentangle(args['num_node_features'],
                           args['num_genes'],
                           args['node_hidden_size'],
                           args['node_embed_size'],
                           args['edge_weights'],
                           args['ae_num_layers'],
                           args['ae_hidden_size'],
                           args['loss_type'],
                           ae_decoder = False,
                           shared_weights = args['shared_weights'],
                           model_backend = args['model_backend'],
                           num_layers = args['gnn_num_layers'])

    elif args['model'] == 'GNN_Disentangle_AE':
        model = GNN_Disentangle(args['num_node_features'],
                           args['num_genes'],
                           args['node_hidden_size'],
                           args['node_embed_size'],
                           args['edge_weights'],
                           args['ae_num_layers'],
                           args['ae_hidden_size'],
                           args['loss_type'],
                           ae_decoder = True,
                           shared_weights = args['shared_weights'],
                           model_backend = args['model_backend'],
                           num_layers = args['gnn_num_layers'])
    elif args['model'] == 'AE':
        model = AE(args['num_node_features'],
                       args['num_genes'],
                       args['node_hidden_size'],
                       args['node_embed_size'],
                       args['ae_num_layers'],
                       args['ae_hidden_size'],
                       args['loss_type'])


    best_model = train(model, pertdl.loaders['train_loader'],
                              pertdl.loaders['val_loader'],
                              pertdl.loaders['edge_index'],
                              pertdl.loaders['edge_attr'],
                              args, device=args["device"])

    print('Start testing....')
    test_res = evaluate(pertdl.loaders['test_loader'],
                            pertdl.loaders['edge_index'],
                            pertdl.loaders['edge_attr'],best_model, args)
    
    test_metrics, test_pert_res = compute_metrics(test_res)
    log = "Final best performing model: Test_DE: {:.4f}, R2 {:.4f} "
    print(log.format(test_metrics['mse_de'], test_metrics['r2_de']))

    if args['wandb']:
        wandb.log({'Test_DE_MSE': test_metrics['mse_de'],
                  'Test_R2': test_metrics['r2_de']})
    print('Saving model....')
        
    # Save model outputs and best model
    np.save('./saved_metrics/'+args['model_name'],test_pert_res)
    np.save('./saved_args/'+ args['model_name'], args)
    torch.save(best_model, './saved_models/' +args['model_name'])
    print('Done!')


def parse_arguments():
    """
    Argument parser
    """

    # dataset arguments
    parser = argparse.ArgumentParser(description='Perturbation response')
    
    parser.add_argument('--dataset', type=str, choices = ['Norman2019'], default="Norman2019")
    parser.add_argument('--split', type=str, choices = ['combo_seen0', 'combo_seen1', 'combo_seen2', 'single', 'single_only'], default="combo_seen0")
    parser.add_argument('--seed', type=int, default=1)    
    parser.add_argument('--test_set_fraction', type=float, default=0.1)
    
    parser.add_argument('--perturbation_key', type=str, default="condition")
    parser.add_argument('--species', type=str, default="human")
    parser.add_argument('--binary_pert', default=True, action='store_false')
    parser.add_argument('--edge_attr', default=True, action='store_false')
    parser.add_argument('--ctrl_remove_train', default=False, action='store_true')
    parser.add_argument('--edge_weights', action='store_true', default=False,
                        help='whether to include linear edge weights during '
                             'GNN training')
    
    # Dataloader related
    parser.add_argument('--pert_feats', default=True, action='store_false',
                        help='Separate feature to indicate perturbation')
    parser.add_argument('--pert_delta', default=False, action='store_true',
                        help='Represent perturbed cells using delta gene '
                             'expression')
    parser.add_argument('--edge_filter', default=False, action='store_true',
                        help='Filter edges based on applied perturbation')
    
    # network arguments
    parser.add_argument('--network_name', type=str, default = 'string')
    parser.add_argument('--top_edge_percent', type=float, default=10,
                        help='percentile of top edges to retain for graph')
    
    # training arguments
    parser.add_argument('--device', type=str, default='cuda')
    parser.add_argument('--max_epochs', type=int, default=20)
    parser.add_argument('--lr', type=float, default=5e-3, help='learning rate')
    parser.add_argument('--lr_decay_step_size', type=int, default=3)
    parser.add_argument('--lr_decay_factor', type=float, default=0.5)
    parser.add_argument('--weight_decay', type=float, default=5e-4)
    parser.add_argument('--batch_size', type=int, default=100)
    parser.add_argument('--print_progress_steps', type=int, default=50)
                        
    # model arguments
    parser.add_argument('--node_hidden_size', type=int, default=2,
                        help='hidden dimension for GNN')
    parser.add_argument('--node_embed_size', type=int, default=1,
                        help='final node embedding size for GNN')
    parser.add_argument('--ae_hidden_size', type=int, default=512,
                        help='hidden dimension for AE')
    parser.add_argument('--gnn_num_layers', type=int, default=2,
                        help='number of layers in GNN')
    parser.add_argument('--ae_num_layers', type=int, default=2,
                        help='number of layers in autoencoder')
    
    parser.add_argument('--model', choices = ['GNN_simple', 'GNN_AE', 'GNN_Disentangle', 'GNN_Disentangle_AE', 'AE'], 
                        type = str, default = 'GNN_AE', help='model name')
    parser.add_argument('--model_backend', choices = ['GCN', 'GAT', 'DeepGCN'], 
                        type = str, default = 'GAT', help='model name')    
    parser.add_argument('--shared_weights', default=False, action='store_true',
                    help='Separate feature to indicate perturbation')                    
                        
    # loss
    parser.add_argument('--pert_loss_wt', type=int, default=1,
                        help='weights for perturbed cells compared to control cells')
    parser.add_argument('--loss_type', type=str, default='micro',
                        help='micro averaged or not')
    parser.add_argument('--loss_mode', choices = ['l2', 'l3'], type = str, default = 'l2')
    parser.add_argument('--focal_gamma', type=int, default=2)    


    
    # wandb related
    parser.add_argument('--wandb', default=False, action='store_true',
                    help='Use wandb or not')
    parser.add_argument('--project_name', type=str, default='pert_gnn',
                        help='project name')
    parser.add_argument('--entity_name', type=str, default='kexinhuang',
                        help='entity name')
    
    return dict(vars(parser.parse_args()))


if __name__ == "__main__":
    trainer(parse_arguments())